from __future__ import division
import os
import argparse
import glob
import sys
import csv
sys.path.append("/Users/mihaileric/Documents/Research/Lasagne")
import lasagne
from lasagne.regularization import regularize_layer_params_weighted, l2, l1, regularize_network_params
import nltk
import numpy as np

# Get top of project path 
if __name__ == '__main__':
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.append(root_dir)

import theano
import theano.tensor as T
import time
from sklearn import metrics
from sklearn.preprocessing import LabelBinarizer
from theano.printing import Print as pp
from util.utils import sick_reader
import featurizer

import warnings
warnings.filterwarnings('ignore', '.*topo.*')

_DEBUG=False

class InnerProductLayer(lasagne.layers.MergeLayer):

    def __init__(self, incomings, nonlinearity=None, **kwargs):
        super(InnerProductLayer, self).__init__(incomings, **kwargs)
        self.nonlinearity = nonlinearity
        if len(incomings) != 2:
            raise NotImplementedError

    def get_output_shape_for(self, input_shapes):
        return input_shapes[0][:2]

    def get_output_for(self, inputs, **kwargs):
        M = inputs[0]
        u = inputs[1]
        # Takes two 3d tensors of (dim1, dim2, dim3) and outputs tensor of (dim1) representing dim1 dot products of the remaining two axes
        output = T.batched_dot(M, u)
        if self.nonlinearity is not None:
            output = self.nonlinearity(output)
        return output


class BatchedDotLayer(lasagne.layers.MergeLayer):

    def __init__(self, incomings, **kwargs):
        super(BatchedDotLayer, self).__init__(incomings, **kwargs)
        if len(incomings) != 2:
            raise NotImplementedError

    def get_output_shape_for(self, input_shapes):
        return (input_shapes[1][0], input_shapes[1][2])

    def get_output_for(self, inputs, **kwargs):
        return T.batched_dot(inputs[0], inputs[1])


class SumLayer(lasagne.layers.Layer):

    def __init__(self, incoming, axis, **kwargs):
        super(SumLayer, self).__init__(incoming, **kwargs)
        self.axis = axis

    def get_output_shape_for(self, input_shape):
        return input_shape[:self.axis] + input_shape[self.axis+1:]

    def get_output_for(self, input, **kwargs):
        return T.sum(input, axis=self.axis)

class ScaleSumLayer(SumLayer):

    def __init__(self, incoming, axis, **kwargs):
        super(SumLayer, self).__init__(incoming, **kwargs)
        self.axis = axis

    def get_output_shape_for(self, input_shape):
        return input_shape[:self.axis] + input_shape[self.axis+1:]

    def get_output_for(self, input, **kwargs):
        return T.mean(input, axis=self.axis)

class GramOverlapLayer():

    def __init__(self, incoming, **kwargs):
        super(GramOverlapLayer, self).__init__(incoming, **kwargs)
        
    def get_output_shape_for(self, input_shape):
        return input_shape

    def get_output_for(self, input, **kwargs):
        return input



# An alternate way to represent  sentences into a vector (m_i in diagram)
class TemporalEncodingLayer(lasagne.layers.Layer):  
    """
    TODO: Force self.T to have width query_len, to apply temporal locality only to adjacent premise-hypothesis pairs.
    """
    def __init__(self, incoming, T=lasagne.init.GlorotNormal(), **kwargs):
        super(TemporalEncodingLayer, self).__init__(incoming, **kwargs)
        self.T = self.add_param(T, self.input_shape[-2:], name="T")

    def get_output_shape_for(self, input_shape):
        return input_shape

    def get_output_for(self, input, **kwargs):
        return input + self.T

# To ensure that W^T (final linear transform) is same as dimension of A, B, C -- will not be relevant for our purposes
# because W will have dim (d x 3) instead of (d x V)
class TransposedDenseLayer(lasagne.layers.DenseLayer):

    def __init__(self, incoming, num_units, W=lasagne.init.GlorotUniform(),
                 b=lasagne.init.Constant(0.), nonlinearity=lasagne.nonlinearities.rectify,
                 **kwargs):
        super(TransposedDenseLayer, self).__init__(incoming, num_units, W, b, nonlinearity, **kwargs)

    def get_output_shape_for(self, input_shape):
        return (input_shape[0], self.num_units)

    def get_output_for(self, input, **kwargs):
        if input.ndim > 2:
            input = input.flatten(2)

        activation = T.dot(input, self.W.T)
        if self.b is not None:
            activation = activation + self.b.dimshuffle('x', 0)
        return self.nonlinearity(activation)


class PositionalEncodingLayer(lasagne.layers.MergeLayer):

    def __init__(self, incomings, vocab, L, **kwargs):
        # Init expects:
        #   l_context_in-- dim (batch_size * max_seq_len * max_sen_len)
        #   l_context_pe_in-- dim (batch_size, max_seq_len, max_sen_len, embed_size)
        #   vocab--
        #   L-- an initialization for the embedding matrix, or None if to use the default initializer.
        super(PositionalEncodingLayer, self).__init__(incomings, **kwargs)
        
        self.vocab = vocab
        if len(incomings) != 2:
            raise NotImplementedError

        self.l_context_in, self.l_context_pe_in = tuple(incomings)
        batch_size, max_seq_len, max_sent_len, embed_size = self.l_context_pe_in.shape
        # This step is important: embedding matrix must be a parameter of this class. 
        self.W = self.add_param(L, (len(vocab) + 1, embed_size), name="W")
        self.embedding = lasagne.layers.EmbeddingLayer(self.l_context_in, len(vocab) + 1, embed_size, W=self.W)

        l_embedding = lasagne.layers.ReshapeLayer(self.embedding, shape=(batch_size, max_seq_len, max_sent_len, embed_size))
        # m_i = sum over j of (l_j * Ax_{ij})
        l_merged = lasagne.layers.ElemwiseMergeLayer((l_embedding, self.l_context_pe_in), merge_function=T.mul)
        self.layer = SumLayer(l_merged, axis=2)
        #---------------------------------------------------

    def get_output_shape_for(self, input_shapes):
        # Output dim: (batch_size, max_seq_len, embed_size)
        _, l_context_pe_shape = tuple(input_shapes)
        return (l_context_pe_shape[0], l_context_pe_shape[1], l_context_pe_shape[3])

    def get_output_for(self, inputs, **kwargs):
        return lasagne.layers.helper.get_output(self.layer, {self.l_context_in: inputs[0], self.l_context_pe_in: inputs[1]})


# Computes the memory network transforms assuming you are given A, C, and query vectors that have been embedded with a B embedding matrix
class MemoryNetworkLayer(lasagne.layers.MergeLayer):

    def __init__(self, incomings, vocab, embedding_size, A, A_T, C, C_T, nonlinearity=lasagne.nonlinearities.softmax, **kwargs):
        super(MemoryNetworkLayer, self).__init__(incomings)
        if len(incomings) != 3:
            raise NotImplementedError

        batch_size, max_seqlen, max_sentlen = self.input_shapes[0]
        query_len = kwargs.get('query_len', 2)
    
        assert(query_len)
        print max_seqlen
        print query_len
        assert(max_seqlen % query_len == 0)

        # Inputs
        #------------------
        l_context_in = lasagne.layers.InputLayer(shape=(batch_size, max_seqlen, max_sentlen))
        l_B_embedding = lasagne.layers.InputLayer(shape=(batch_size, query_len*embedding_size))
        l_context_pe_in = lasagne.layers.InputLayer(shape=(batch_size, max_seqlen, max_sentlen, embedding_size))
        #------------------

        # generate the temporal encoding of sentence sequences using per-word positional encoding which is l_context_pe_in
        pe_A_encoding = PositionalEncodingLayer([l_context_in, l_context_pe_in], vocab, A)
        self.A = pe_A_encoding.W
        #------------
        l_A_embedding = TemporalEncodingLayer(pe_A_encoding, T=A_T)
        self.A_T = l_A_embedding.T
        l_A_embedding = lasagne.layers.ReshapeLayer(l_A_embedding, shape=(batch_size, int(max_seqlen / query_len), query_len * embedding_size))
        
        pe_C_embedding = PositionalEncodingLayer([l_context_in, l_context_pe_in], vocab, C)
        self.C = pe_C_embedding.W
        #------------
        l_C_embedding = TemporalEncodingLayer(pe_C_embedding, T=C_T)
        self.C_T = l_C_embedding.T
        l_C_embedding = lasagne.layers.ReshapeLayer(l_C_embedding, shape=(batch_size, int(max_seqlen / query_len), query_len * embedding_size))


        # Performs attention to get p_i = softmax (u^T * m_i)
        l_prob = InnerProductLayer((l_A_embedding, l_B_embedding), nonlinearity=nonlinearity)

        # gives o = sum (p_i * c_i) from paper
        l_weighted_output = BatchedDotLayer((l_prob, l_C_embedding))

        # o + u from paper
        l_sum = lasagne.layers.ElemwiseSumLayer((l_weighted_output, l_B_embedding))

        self.l_context_in = l_context_in
        self.l_B_embedding = l_B_embedding
        self.l_context_pe_in = l_context_pe_in
        self.network = l_sum

        params = lasagne.layers.helper.get_all_params(self.network, trainable=True)
        values = lasagne.layers.helper.get_all_param_values(self.network, trainable=True)
        for p, v in zip(params, values):
            self.add_param(p, v.shape, name=p.name)


        # Add bias vector because of len(vocab) + 1 from above
        zero_vec_tensor = T.vector()
        self.zero_vec = np.zeros(embedding_size, dtype=theano.config.floatX)
        self.set_zero = theano.function([zero_vec_tensor], updates=[(x, T.set_subtensor(x[0, :], zero_vec_tensor)) for x in [self.A, self.C]])

    def get_output_shape_for(self, input_shapes):
        # Need the output shape to be (batch_size, 2*hidden_size)
        return lasagne.layers.helper.get_output_shape(self.network)

    def get_output_for(self, inputs, **kwargs):
        return lasagne.layers.helper.get_output(self.network, {self.l_context_in: inputs[0], self.l_B_embedding: inputs[1], self.l_context_pe_in: inputs[2]})

    def reset_zero(self):
        self.set_zero(self.zero_vec)


class Model:

    def __init__(self, train_file, test_file, batch_size=32, 
                embedding_size=20, max_norm=40, lr=0.01, num_hops=1, 
                adj_weight_tying=True, linear_start=True, **kwargs):

        filenames = (root_dir + "/data/SICK/SICK_train_parsed.txt", root_dir + "/data/SICK/SICK_dev_parsed.txt", 
                     root_dir + "/data/SICK/SICK_test_parsed.txt")
        reader = sick_reader
        vocab, word_to_idx, idx_to_word, max_seqlen, max_sentlen = get_vocab(filenames, reader)


        train_labels, train_lines = parse_SICK(filenames[0], word_to_idx)
        test_labels, test_lines = parse_SICK(filenames[1], word_to_idx)
        lines = np.concatenate([train_lines, test_lines], axis=0)

        self.data = {'train': {}, 'test': {}}
        S_train, self.data['train']['C'], self.data['train']['Q'], self.data['train']['Y'] = self.process_dataset(train_lines, word_to_idx, max_sentlen, train_labels)
        train_len = S_train.shape[0]

        S_test, self.data['test']['C'], self.data['test']['Q'], self.data['test']['Y'] = self.process_dataset(test_lines, word_to_idx, max_sentlen,        
                                                                                                      test_labels, offset=train_len)
        S = np.concatenate([S_train, S_test], axis=0)

        lb = LabelBinarizer()
        lb.fit(train_labels + test_labels)
        
        self.batch_size = batch_size
        self.max_seqlen = max_seqlen
        print "-" * 80
        self.query_len = kwargs['query_len']


        self.max_sentlen = max_sentlen
        self.embedding_size = 100
        self.num_classes = 3 #len(vocab) + 1
        self.vocab = vocab
        self.adj_weight_tying = adj_weight_tying
        self.num_hops = num_hops
        self.lb = lb
        self.init_lr = lr
        self.lr = self.init_lr
        self.max_norm = max_norm
        self.S = S
        self.idx_to_word = idx_to_word
        self.word_to_idx = word_to_idx
        self.nonlinearity = None if linear_start else lasagne.nonlinearities.softmax

        self.build_simple_lstm()
        print "Network built"

    
    def build_glove_embedding(self, filepath, hidden_size):
        """ 
        Builds a glove vector table for the embeddings from the given filename.
        """

        reader = csv.reader(file(filepath), delimiter=' ', quoting=csv.QUOTE_NONE)
        colnames = None
        print "building glove vectors..."
        # Kaiming-He
        mat = 0.2*np.random.randn(len(self.word_to_idx) + 1, hidden_size)

        for line in reader:        
            if line[0] in self.word_to_idx: 
                idx = self.word_to_idx[line[0]]
                mat[idx, :] = np.array(map(float, line[1: ]))
        
        print "Done building vectors"
        return mat

    def build_logistic_network(self):
        """
        Builds a super simple logistic regression on top of a featurizer layer. 
        """

        batch_size, max_seqlen, max_sentlen, embedding_size, vocab = self.batch_size, self.max_seqlen, self.max_sentlen, self.embedding_size, self.vocab
        self.hidden_size = 256
        c = T.imatrix()
        y = T.imatrix()

        self.c_shared = theano.shared(np.zeros((batch_size,), dtype=np.int32), borrow=True)
        self.a_shared = theano.shared(np.zeros((batch_size, self.num_classes), dtype=np.int32), borrow=True)

        S_shared = theano.shared(self.S, borrow=True)
        cc = S_shared[c.flatten()].reshape((batch_size, max_seqlen, max_sentlen))

        l_context_in = lasagne.layers.InputLayer(shape=(batch_size, max_seqlen, max_sentlen))
        

        #quit()
    def build_simple_network(self):
        """
        Builds a very, very simple non-memory network to assess the effectiveness of training on the task.
        - Input: (batch_size, max_seqlen, max_sentlen)
        - Wordwise embedding into (batch_size, max_seqlen, max_sentlen, embed_size) 
        - Sum all words in a sentence: (batch_size, max_seqlen, embed_size)
        - Reshape embedding into (batch_size, max_seqlen * embed_size)
        - 3 hidden layers with sigmoid, hidden dim (512, 512, 256)
        """

        batch_size, max_seqlen, max_sentlen, embedding_size, vocab = self.batch_size, self.max_seqlen, self.max_sentlen, self.embedding_size, self.vocab
       
        self.hidden_size = 256
        c = T.imatrix()
        y = T.imatrix()

        self.c_shared = theano.shared(np.zeros((batch_size, max_seqlen), dtype=np.int32), borrow=True)
        self.a_shared = theano.shared(np.zeros((batch_size, self.num_classes), dtype=np.int32), borrow=True)

        S_shared = theano.shared(self.S, borrow=True)
        cc = S_shared[c.flatten()].reshape((batch_size, max_seqlen, max_sentlen))

        l_context_in = lasagne.layers.InputLayer(shape=(batch_size, max_seqlen, max_sentlen))


        L = self.build_glove_embedding(root_dir + "/data/glove/glove.6B.100d.txt", hidden_size=embedding_size)
        print L
       
        embedding = lasagne.layers.EmbeddingLayer(l_context_in, len(vocab) + 1, embedding_size, W=L)

        sum_embeddings = ScaleSumLayer(embedding, axis=2)
        
        reshape_sum = lasagne.layers.ReshapeLayer(sum_embeddings, shape=(batch_size, max_seqlen*embedding_size))
        # Fully connected layers

        dense_1 = lasagne.layers.DenseLayer(reshape_sum, self.hidden_size , W=lasagne.init.GlorotNormal(), 
                                            nonlinearity=T.nnet.sigmoid)
        dense_2 = lasagne.layers.DenseLayer(dense_1, self.hidden_size,  W=lasagne.init.GlorotNormal(),
                                            nonlinearity=T.nnet.sigmoid)
        l_pred = lasagne.layers.DenseLayer(dense_2, self.num_classes, nonlinearity=lasagne.nonlinearities.softmax)

        rand_in = np.random.randint(0, len(vocab) - 1, size=(batch_size, max_seqlen, max_sentlen))
     
        fake_probs = lasagne.layers.get_output(l_pred, {l_context_in : rand_in} ).eval()
        print "fake_probs: ", fake_probs  

        probas = lasagne.layers.helper.get_output(l_pred, {l_context_in: cc})
     
        pred = T.argmax(probas, axis=1)
   
        # l2 regularization
        reg_coeff = 1e-1
        p_metric = l2
        layer_dict = {dense_1: reg_coeff, dense_2 : reg_coeff, l_pred : reg_coeff}
        reg_cost = reg_coeff * regularize_layer_params_weighted(layer_dict, p_metric)

        cost = T.nnet.categorical_crossentropy(probas, y).mean() #+ reg_cost

        params = lasagne.layers.helper.get_all_params(l_pred, trainable=True)
        grads = T.grad(cost, params)

        scaled_grads = lasagne.updates.total_norm_constraint(grads, self.max_norm)
        updates = lasagne.updates.adam(scaled_grads, params, learning_rate=self.lr)

        givens = {
            c: self.c_shared,
            y: self.a_shared,
        }

        self.train_model = theano.function([], cost, givens=givens, updates=updates, on_unused_input='ignore')
        self.compute_pred = theano.function([], pred, givens=givens, on_unused_input='ignore')

        zero_vec_tensor = T.vector()
        self.zero_vec = np.zeros(embedding_size, dtype=theano.config.floatX)
        self.set_zero = theano.function([zero_vec_tensor], on_unused_input='ignore')

        #self.nonlinearity = nonlinearity
        self.network = l_pred

    def build_simple_lstm(self):
        """ 
        Builds an LSTM encoder / decoder.
        """
        print "=" * 80 
        print "BUILDING single-layer LSTM"
        print "=" * 80

        batch_size, max_seqlen, max_sentlen, embedding_size, vocab = self.batch_size, self.max_seqlen, self.max_sentlen, self.embedding_size, self.vocab
        self.hidden_size = 256
        c = T.imatrix()
        y = T.imatrix()

        self.c_shared = theano.shared(np.zeros((batch_size, max_seqlen), dtype=np.int32), borrow=True)
        self.a_shared = theano.shared(np.zeros((batch_size, self.num_classes), dtype=np.int32), borrow=True)

        S_shared = theano.shared(self.S, borrow=True)
        # Indexed in sentences
        cc = S_shared[c.flatten()].reshape((batch_size, max_seqlen, max_sentlen))

        l_context_in = lasagne.layers.InputLayer(shape=(batch_size, max_seqlen, max_sentlen))

        L = self.build_glove_embedding(root_dir + "/data/glove/glove.6B.100d.txt", hidden_size=embedding_size)
       
        embedding = lasagne.layers.EmbeddingLayer(l_context_in, len(vocab) + 1, embedding_size, W=L)
        sum_embeddings = ScaleSumLayer(embedding, axis=2)
        # Force embeddings to (batch_size, max_seqlen, embedding_size)
        l_sequence = lasagne.layers.ReshapeLayer(sum_embeddings, shape=(batch_size, max_seqlen, embedding_size))
        print "compiling lstm"
        l_lstm = lasagne.layers.LSTMLayer(
                l_sequence, self.hidden_size, 
                grad_clipping=100, gradient_steps=-1, 
                unroll_scan=True,
                nonlinearity=T.tanh,
                only_return_final=True)
        print 'done compiling'
        #l_forward = lasagne.layers.SliceLayer(l_lstm, -1, 1)

        l_pred = lasagne.layers.DenseLayer(l_lstm, num_units=self.num_classes,
                    nonlinearity=lasagne.nonlinearities.softmax)

        probas = lasagne.layers.helper.get_output(l_pred, {l_context_in: cc})
            

        pred = T.argmax(probas, axis=1)
        cost = T.nnet.categorical_crossentropy(probas, y).sum() #+ reg_cost

        params = lasagne.layers.helper.get_all_params(l_pred, trainable=True)
        grads = T.grad(cost, params)

        scaled_grads = lasagne.updates.total_norm_constraint(grads, self.max_norm)
        updates = lasagne.updates.adam(scaled_grads, params, learning_rate=self.lr)

        givens = {
            c: self.c_shared,
            y: self.a_shared,
        }

        self.train_model = theano.function([], cost, givens=givens, updates=updates, on_unused_input='warn')
        self.compute_pred = theano.function([], pred, givens=givens, on_unused_input='ignore')

        zero_vec_tensor = T.vector()
        self.zero_vec = np.zeros(embedding_size, dtype=theano.config.floatX)
        self.set_zero = theano.function([zero_vec_tensor], on_unused_input='ignore')

        #self.nonlinearity = nonlinearity
        self.network = l_pred

    def build_network(self, nonlinearity):
        batch_size, max_seqlen, max_sentlen, embedding_size, vocab = self.batch_size, self.max_seqlen, self.max_sentlen, self.embedding_size, self.vocab

        # index form of questions for bAbI
        # {x_i}
        c = T.imatrix()
        # query
        q = T.imatrix()
        # label
        y = T.imatrix()

        # positional encoding of context/query
        c_pe = T.tensor4()
        q_pe = T.tensor4()

        self.c_shared = theano.shared(np.zeros((batch_size, max_seqlen), dtype=np.int32), borrow=True)
        # represents premise/hypothesis pair hence why axis 1 has value 2
        self.q_shared = theano.shared(np.zeros((batch_size, self.query_len), dtype=np.int32), borrow=True)
        self.a_shared = theano.shared(np.zeros((batch_size, self.num_classes), dtype=np.int32), borrow=True)
        self.c_pe_shared = theano.shared(np.zeros((batch_size, max_seqlen, max_sentlen, embedding_size), dtype=theano.config.floatX), borrow=True)
        self.q_pe_shared = theano.shared(np.zeros((batch_size, self.query_len, max_sentlen, embedding_size), dtype=theano.config.floatX), borrow=True)
        
        # the repository of sentences to index into
        S_shared = theano.shared(self.S, borrow=True)
        
        # Actual embeddings of contexts and queries
        # hard-coded 2 to indicate premise/hypothesis pair
        cc = S_shared[c.flatten()].reshape((batch_size, max_seqlen, max_sentlen))
        qq = S_shared[q.flatten()].reshape((batch_size, self.query_len, max_sentlen))

        l_context_in = lasagne.layers.InputLayer(shape=(batch_size, max_seqlen, max_sentlen))
        l_question_in = lasagne.layers.InputLayer(shape=(batch_size, self.query_len, max_sentlen))

        l_context_pe_in = lasagne.layers.InputLayer(shape=(batch_size, max_seqlen, max_sentlen, embedding_size))
        # premise/hypothesis pair
        l_question_pe_in = lasagne.layers.InputLayer(shape=(batch_size, self.query_len, max_sentlen, embedding_size))

        A, C = lasagne.init.GlorotNormal().sample((len(vocab)+1, embedding_size)), lasagne.init.GlorotNormal()
        A_T, C_T, B_T = lasagne.init.GlorotNormal(), lasagne.init.GlorotNormal(), lasagne.init.GlorotNormal()
        W = A if self.adj_weight_tying else lasagne.init.GlorotNormal()

        # premise/hypothesis pair
        l_question_in = lasagne.layers.ReshapeLayer(l_question_in, shape=(batch_size * self.query_len * max_sentlen, ))
        # Positional Embedding for b
        l_B_embedding = lasagne.layers.EmbeddingLayer(l_question_in, len(vocab)+1, embedding_size, W=W)
        B = l_B_embedding.W
        # premise/hypothesis pair
        l_B_embedding = lasagne.layers.ReshapeLayer(l_B_embedding, shape=(batch_size, self.query_len, max_sentlen, embedding_size))
        l_B_embedding = lasagne.layers.ElemwiseMergeLayer((l_B_embedding, l_question_pe_in), merge_function=T.mul)

        #l_B_embedding = lasagne.layers.ReshapeLayer(l_B_embedding, shape=(batch_size, max_sentlen, embedding_size))
        l_B_embedding = SumLayer(l_B_embedding, axis=2)
        l_B_embedding = TemporalEncodingLayer(l_B_embedding, T=B_T)
        self.B_T = l_B_embedding.T
        l_B_embedding = lasagne.layers.ReshapeLayer(l_B_embedding, shape=(batch_size, self.query_len*embedding_size))

        self.mem_layers = [MemoryNetworkLayer((l_context_in, l_B_embedding, l_context_pe_in), vocab, embedding_size, 
                                             A=A, A_T=A_T, C=C, C_T=C_T, nonlinearity=nonlinearity, 
                                             **{'query_len' : self.query_len})]
        
        for _ in range(1, self.num_hops):

            if self.adj_weight_tying:
                print "*** RUNNING IN WEIGHT_TIED MODE ***"
                A, C = self.mem_layers[-1].C, lasagne.init.GlorotNormal()
                A_T, C_T = self.mem_layers[-1].C_T, lasagne.init.GlorotNormal()
            else:  # RNN style
                print "*** RUNNING IN RNN MODE *** "
                A, C = self.mem_layers[-1].A, self.mem_layers[-1].C
                A_T, C_T = self.mem_layers[-1].A_T, self.mem_layers[-1].C_T

            self.mem_layers += [MemoryNetworkLayer((l_context_in, self.mem_layers[-1], l_context_pe_in), vocab, embedding_size, 
                                A=A, A_T=A_T, C=C, C_T=C_T, nonlinearity=nonlinearity, 
                                **{'query_len' : self.query_len})]

        # if self.adj_weight_tying:
        #     l_pred = TransposedDenseLayer(self.mem_layers[-1], self.num_classes, W=self.mem_layers[-1].C, b=None, nonlinearity=lasagne.nonlinearities.softmax)
        # else:
        # must output to num_labels
        l_pred = lasagne.layers.DenseLayer(self.mem_layers[-1], self.num_classes, W=lasagne.init.GlorotNormal(), b=None, nonlinearity=lasagne.nonlinearities.softmax)

        probas = lasagne.layers.helper.get_output(l_pred, {l_context_in: cc, l_question_in: qq, l_context_pe_in: c_pe, l_question_pe_in: q_pe})
        probas = T.clip(probas, 1e-7, 1.0-1e-7)

        pred = T.argmax(probas, axis=1)

        cost = T.nnet.categorical_crossentropy(probas, y).sum()

        params = lasagne.layers.helper.get_all_params(l_pred, trainable=True)
        grads = T.grad(cost, params)
        scaled_grads = lasagne.updates.total_norm_constraint(grads, self.max_norm)
        updates = lasagne.updates.adam(scaled_grads, params, learning_rate=self.lr)

        givens = {
            c: self.c_shared,
            q: self.q_shared,
            y: self.a_shared,
            c_pe: self.c_pe_shared,
            q_pe: self.q_pe_shared
        }

        self.train_model = theano.function([], cost, givens=givens, updates=updates, on_unused_input='warn')
        self.compute_pred = theano.function([], pred, givens=givens, on_unused_input='warn')

        zero_vec_tensor = T.vector()
        self.zero_vec = np.zeros(embedding_size, dtype=theano.config.floatX)
        self.set_zero = theano.function([zero_vec_tensor], updates=[(x, T.set_subtensor(x[0, :], zero_vec_tensor)) for x in [B]])

        self.nonlinearity = nonlinearity
        self.network = l_pred

    def reset_zero(self): 
        if not hasattr(self, 'mem_layers'):
            return
        self.set_zero(self.zero_vec)
        for l in self.mem_layers:
            l.reset_zero()

    def predict(self, dataset, index):
        self.set_shared_variables(dataset, index)
        return self.compute_pred()

    def compute_f1(self, dataset):
        n_batches = len(dataset['Y']) // self.batch_size
        y_pred = np.concatenate([self.predict(dataset, i) for i in xrange(n_batches)]).astype(np.int32) - 1

        y_true = self.lb.transform([y for y in dataset['Y'][:len(y_pred)]])

        # Convert back to single label representation
        y_true = y_true.argmax(axis=1)
        y_true = y_true.reshape(len(y_pred))
        y_true_confuse = [int(out) - 1 for out in y_true]

        y_true = np.array(y_true_confuse) 

        print metrics.confusion_matrix(y_true, y_pred)
        print metrics.classification_report(y_true, y_pred)
        errors = []
        for i, (t, p) in enumerate(zip(y_true, y_pred)):
            if t != p:
                errors.append((i, self.lb.classes_[p]))
        return metrics.f1_score(y_true, y_pred, average='weighted', pos_label=None), errors

    def train(self, n_epochs=100, shuffle_batch=False):
        ''' 
        Trains the network for n_epochs, with learning_rate annealing, and 
        verbose printing at each epoch.
        '''
        epoch = 0
        n_train_batches = len(self.data['train']['Y']) // self.batch_size
        self.lr = self.init_lr
        prev_train_f1 = None

        while (epoch < n_epochs):
            epoch += 1

            if epoch % 25 == 0:
                self.lr /= 2.0

            indices = range(n_train_batches)
            if shuffle_batch:
                self.shuffle_sync(self.data['train'])

            total_cost = 0
            start_time = time.time()
            for minibatch_index in indices:
                self.set_shared_variables(self.data['train'], minibatch_index)
                total_cost += self.train_model()
                self.reset_zero()
            end_time = time.time()
            print '\n' * 3, '*' * 80
            print 'epoch:', epoch, 'cost:', (total_cost / len(indices)), ' took: %d(s)' % (end_time - start_time)

            print 'TRAIN', '=' * 40
            train_f1, train_errors = self.compute_f1(self.data['train'])
            print 'TRAIN_ERROR:', (1-train_f1)*100
            if False:
                for i, pred in train_errors[:10]:
                    print 'context: ', self.to_words(self.data['train']['C'][i])
                    print 'question: ', self.to_words([self.data['train']['Q'][i]])
                    print 'correct answer: ', self.data['train']['Y'][i]
                    print 'predicted answer: ', pred
                    print '---' * 20

            if prev_train_f1 is not None and train_f1 < prev_train_f1 and self.nonlinearity is None:
                prev_weights = lasagne.layers.helper.get_all_param_values(self.network)
                self.build_network(nonlinearity=lasagne.nonlinearities.softmax)
                lasagne.layers.helper.set_all_param_values(self.network, prev_weights)
            else:
                print 'TEST', '=' * 40
                test_f1, test_errors = self.compute_f1(self.data['test'])
                print '*** TEST_ERROR:', (1-test_f1)*100

            prev_train_f1 = train_f1

    def to_words(self, indices):
        sents = []
        for idx in indices:
            words = ' '.join([self.idx_to_word[idx] for idx in self.S[idx] if idx > 0])
            sents.append(words)
        return ' '.join(sents)

    def shuffle_sync(self, dataset):
        p = np.random.permutation(len(dataset['Y']))
        for k in ['C', 'Q', 'Y']:
            dataset[k] = dataset[k][p]

    def set_shared_variables(self, dataset, index):
        """
        Sets the shared variables before runnning the network.
        Inputs:
        - dataset: a dictionary with keys 'C', 'Q', 'Y', containing padded context, query, and output sentence indices,

        """
        # Commented out for simpler networks

        c = np.zeros((self.batch_size, self.max_seqlen), dtype=np.int32)
        # This represents a premise/hypothesis pair, hence why axis 1 has dim 2
        # q = np.zeros((self.batch_size, self.query_len), dtype=np.int32)
        y = np.zeros((self.batch_size, self.num_classes), dtype=np.int32)
        # c_pe = np.zeros((self.batch_size, self.max_seqlen, self.max_sentlen, self.embedding_size), dtype=theano.config.floatX)
        # q_pe = np.zeros((self.batch_size, self.query_len, self.max_sentlen, self.embedding_size), dtype=theano.config.floatX)

        indices = range(index*self.batch_size, (index+1)*self.batch_size)
        for i, row in enumerate(dataset['C'][indices]):
            row = row[:self.max_seqlen]
            c[i, :len(row)] = row

        # q[:len(indices)] = dataset['Q'][indices]
        # Create the positional encoding

        # for key, mask in [('C', c_pe), ('Q', q_pe)]:
        #     for i, row in enumerate(dataset[key][indices]):
        #         sentences = self.S[row].reshape((-1, self.max_sentlen))
        #         for ii, word_idxs in enumerate(sentences):
        #             J = np.count_nonzero(word_idxs)
        #             for j in np.arange(J):
        #                 mask[i, ii, j, :] = (1 - (j+1)/J) - ((np.arange(self.embedding_size)+1)/self.embedding_size)*(1 - 2*(j+1)/J)


        y[:len(indices), :] = self.lb.transform(dataset['Y'][indices])

        self.c_shared.set_value(c)
        # self.q_shared.set_value(q)
        self.a_shared.set_value(y)
        # self.c_pe_shared.set_value(c_pe)
        # self.q_pe_shared.set_value(q_pe)

    def process_dataset(self, lines, word_to_idx, max_sentlen, labels, offset=0):
        """ 
        TODO: pass in lines simply as a list of list of tokens, not a list of {text: ... , type: ...}
        knowing the premise, hypothesis ordering
        TODO: pass in the labels list too
        """

        S, C, Q, Y = [], [], [], []

        for i, line in enumerate(lines):
            word_indices = [word_to_idx[w] for w in line]
            padding = [0] * (max_sentlen - len(word_indices))
            # front padding for LSTM
            word_indices = padding + word_indices
            
            S.append(word_indices)
            
            if i % 2:
                C.append([i + offset, i + offset -1])
                Q.append([i + offset, i + offset -1])
                #indices = [i-j for j in np.arange(2, 22)]
            #     # premise-hypothesis adding

            #     print "premise/hypothesis: ", lines[i], " ", lines[i-1], " ", i
            #     print len(lines)
                # One label per every two examples
            

        return (np.array(S, dtype=np.int32), 
               np.array(C), np.array(Q, dtype=np.int32), 
               np.array(labels))

    def get_lines(self, fname):
        lines = []
        for i, line in enumerate(open(fname)):
            id = int(line[0:line.find(' ')])
            line = line.strip()
            line = line[line.find(' ')+1:]
            if line.find('?') == -1:
                lines.append({'type': 's', 'text': line})
            else:
                idx = line.find('?')
                tmp = line[idx+1:].split('\t')
                lines.append({'id': id, 'type': 'q', 'text': line[:idx], 'answer': tmp[1].strip(), 'refs': [int(x)-1 for x in tmp[2:][0].split(' ')]})
            if False and i > 1000:
                break
        return np.array(lines)


def str2bool(v):
    return v.lower() in ('yes', 'true', 't', '1')

def get_vocab(filenames, reader):
        """ 
        Inputs:
        - filenames: a list of files to look through to form the vocabulary
        - reader: a reader function that takes in the filename and emits a line-by-line tuple 
          of (label, premise, hypothesis) for each ex.
        """
        vocab = set()
        max_sentlen = 0

        for fname in filenames:
            for _, p, h in reader(fname):
                for line in [p, h]:
                    max_sentlen = max(max_sentlen, len(line))
                    for w in line:
                        vocab.add(w)
           
        word_to_idx = {} 
        for w in vocab:
            word_to_idx[w] = len(word_to_idx) + 1

        idx_to_word = {}
        for w, idx in word_to_idx.iteritems():
            idx_to_word[idx] = w

        max_seqlen = 2 # premise-hypothesis pairs only
        return vocab, word_to_idx, idx_to_word, max_seqlen, max_sentlen


def parse_SICK(filename, word_to_idx):
    """ 
    Prototype method to parse the SICK dataset into the format for MemN2N.
    Inputs:
    - filename: the SICK file to parse
    - word_to_idx: a comprehensive vocabulary mapping
    """
    labels, sentences = [], []
    for l, premise, hypothesis in sick_reader(filename):
        sentences.append(premise)
        sentences.append(hypothesis)
        labels.append(l)

    # Overfit on smaller dataset
    #return labels[:200], sentences[:400]
    return labels, sentences



def main():
    parser = argparse.ArgumentParser()
    parser.register('type', 'bool', str2bool)
    parser.add_argument('--train_file', type=str, default='', help='Train file')
    parser.add_argument('--test_file', type=str, default='', help='Test file')
    parser.add_argument('--batch_size', type=int, default=8, help='Batch size')
    parser.add_argument('--embedding_size', type=int, default=20, help='Embedding size')
    parser.add_argument('--max_norm', type=float, default=40.0, help='Max norm')
    parser.add_argument('--lr', type=float, default=0.01, help='Learning rate')
    parser.add_argument('--num_hops', type=int, default=3, help='Num hops')
    parser.add_argument('--adj_weight_tying', type='bool', default=True, help='Whether to use adjacent weight tying')
    parser.add_argument('--linear_start', type='bool', default=False, help='Whether to start with linear activations')
    parser.add_argument('--shuffle_batch', type='bool', default=False, help='Whether to shuffle minibatches')
    parser.add_argument('--n_epochs', type=int, default=100, help='Num epochs')
    parser.add_argument('--l2_reg', type=float, default=0.01, help='l2 regularization')
    parser.add_argument('--query_len', type=int, default=2, help='max Length of network query' )
    args = parser.parse_args()
    print '*' * 80
    print 'args:', args
    print '*' * 80

    model = Model(**args.__dict__)
    model.train(n_epochs=args.n_epochs, shuffle_batch=args.shuffle_batch)

if __name__ == '__main__':

    if _DEBUG:
        small_test()
    else:
        main()
