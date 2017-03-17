
import numpy as np
import tensorflow as tf
import tensorflow.contrib.layers as tfl

MOMENTUM_INIT = 0.5

class Model:

    def init_inference(self, config):
        self.output_dim = num_labels = config['output_dim']
        self.batch_size = batch_size = config['batch_size']

        self.inputs = inputs = tf.placeholder(tf.float32, shape=(batch_size, None))
        acts = tf.reshape(inputs, (batch_size, -1, 1, 1))

        stride_prod = 1
        for layer in config['conv_layers']:
            num_filters = layer['num_filters']
            kernel_size = layer['kernel_size']
            stride = layer['stride']
            stride_prod *= stride
            acts = tfl.convolution2d(acts, num_outputs=num_filters,
                                     kernel_size=[kernel_size, 1],
                                     stride=stride)
        # TODO, awni, this is needed to put the input into the
        # frequency of the output (e.g. 200hz ->1hz)
        assert stride_prod == 200, "Bad overall subsample factor."

        # Activations should emerge from the convolution with shape
        # [batch_size, time (subsampled), 1, num_channels]
        acts = tf.squeeze(acts, squeeze_dims=[2])

        rnn_conf = config.get('rnn', None)
        if rnn_conf is not None:
            bidirectional = rnn_conf.get('bidirectional', False)
            rnn_dim = rnn_conf['dim']
            cell_type = rnn_conf.get('cell_type', 'gru')
            if bidirectional:
                acts = _bi_rnn(acts, rnn_dim, cell_type)
            else:
                acts = _rnn(acts, rnn_dim, cell_type)

        self.logits = tfl.fully_connected(acts, self.output_dim)
        self.probs = tf.nn.softmax(self.logits)

    def init_loss(self):
        self.mask = tf.placeholder(tf.float32,
                        shape=(self.batch_size, None))
        total_inv = (1.0 / tf.reduce_sum(self.mask))

        self.labels = tf.placeholder(tf.int64, shape=(self.batch_size, None))
        losses = tf.nn.sparse_softmax_cross_entropy_with_logits(
                    self.logits, self.labels)
        losses = self.mask * losses
        self.loss =  total_inv * tf.reduce_sum(losses)

        correct = tf.equal(tf.argmax(self.logits, 2), self.labels)
        correct = self.mask * tf.cast(correct, tf.float32)
        self.acc = total_inv * tf.reduce_sum(correct)

    def init_train(self, config):

        l2_weight = config.get('l2_weight', None)
        if l2_weight is not None:
            # *NB* assumes we want an l2 penalty for all trainable variables.
            l2s = [tf.nn.l2_loss(p) for p in tf.trainable_variables()]
            self.loss += l2_weight * tf.add_n(l2s)

        self.momentum = config['momentum']
        self.mom_var = tf.Variable(MOMENTUM_INIT, trainable=False,
                                   dtype=tf.float32)
        ema = tf.train.ExponentialMovingAverage(0.95)
        ema_op = ema.apply([self.loss, self.acc])
        self.avg_loss = ema.average(self.loss)
        self.avg_acc = ema.average(self.acc)

        tf.scalar_summary("Loss", self.loss)
        tf.scalar_summary("Accuracy", self.acc)

        self.it = tf.Variable(0, trainable=False, dtype=tf.int64)

        learning_rate = tf.train.exponential_decay(float(config['learning_rate']),
                            self.it, config['decay_steps'],
                            config['decay_rate'], staircase=True)

        optimizer = tf.train.MomentumOptimizer(learning_rate, self.mom_var)

        gvs = optimizer.compute_gradients(self.loss)

        # Gradient clipping
        clip_norm = config.get('clip_norm', None)
        if clip_norm is not None:
            tf.clip_by_global_norm([g for g, _ in gvs], clip_norm=clip_norm)

        train_op = optimizer.apply_gradients(gvs, global_step=self.it)
        with tf.control_dependencies([train_op]):
            self.train_op = tf.group(ema_op)

    def set_momentum(self, session):
        self.mom_var.assign(self.momentum).eval(session=session)

    def feed_dict(self, inputs, labels=None):
        """
        Generates a feed dictionary for the model's place-holders.
        *NB* inputs and labels are assumed to all be of the same
        lenght.
        Params:
            inputs : List of 1D arrays of wave segments
            labels (optional) : List of lists of integer labels
        Returns:
            feed_dict (use with feed_dict kwarg in session.run)
        """
        feed_dict = {self.inputs : np.vstack(inputs)}
        if labels is not None:
            feed_dict[self.labels] = np.vstack(labels)
            feed_dict[self.mask] = np.ones((len(labels), len(labels[0])))
        return feed_dict

def _rnn(acts, input_dim, cell_type, scope=None):
    if cell_type == 'gru':
        cell = tf.nn.rnn_cell.GRUCell(input_dim)
    elif cell_type == 'lstm':
        cell = tf.nn.rnn_cell.LSTMCell(input_dim)
    else:
        msg = "Invalid cell type {}".format(cell_type)
        raise ValueError(msg)

    acts, _ = tf.nn.dynamic_rnn(cell, acts,
                  dtype=tf.float32, scope=scope)
    return acts

def _bi_rnn(acts, input_dim, cell_type):
    """
    For some reason tf.bidirectional_dynamic_rnn requires a sequence length.
    """
    # Forwards
    with tf.variable_scope("fw") as fw_scope:
        acts_fw = _rnn(acts, input_dim, cell_type,
                       scope=fw_scope)

    # Backwards
    with tf.variable_scope("bw") as bw_scope:
        reverse_dims = [False, True, False]
        acts_bw = tf.reverse(acts, dims=reverse_dims)
        acts_bw = _rnn(acts_bw, input_dim, cell_type,
                       scope=bw_scope)
        acts_bw = tf.reverse(acts_bw, dims=reverse_dims)

    # Sum the forward and backward states.
    return tf.add(acts_fw, acts_bw)

