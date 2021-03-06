# Copyright 2017 reinforce.io. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""
Models provide the general interface to TensorFlow functionality,
manages TensorFlow session and execution. In particular, a agent for reinforcement learning
always needs to provide a function that gives an action, and one to trigger updates.
A agent may use one more multiple neural networks and implement the update logic of a particular RL algorithm.
"""

from __future__ import absolute_import
from __future__ import print_function
from __future__ import division

import logging
import tensorflow as tf

from tensorforce import TensorForceError, util
from tensorforce.core.optimizers import Optimizer
from tensorforce.util import SummarySessionWrapper


class Model(object):
    """
    Base model class.

    Each model requires the following configuration parameters:

    * `discount`: float of discount factor (gamma).
    * `learning_rate`: float of learning rate (alpha).
    * `optimizer`: string of optimizer to use (e.g. 'adam').
    * `device`: string of tensorflow device name.
    * `tf_summary`: string directory to write tensorflow summaries. Default None
    * `tf_summary_level`: int indicating which tensorflow summaries to create.
    * `tf_summary_interval`: int number of calls to get_action until writing tensorflow summaries on update.
    * `log_level`: string containing log level (e.g. 'info').
    * `distributed`: boolean indicating whether to use distributed tensorflow.
    * `global_model`: global model.
    * `session`: session to use.

    """
    allows_discrete_actions = None
    allows_continuous_actions = None

    default_config = dict(
        discount=0.97,
        learning_rate=0.0001,
        optimizer='adam',
        device=None,
        tf_summary=None,
        tf_summary_level=0,
        tf_summary_interval=1000,
        distributed=False,
        global_model=False,
        session=None
    )

    def __init__(self, config):
        """
        Creates a base reinforcement learning model with the specified configuration. Manages the creation
        of TensorFlow operations and provides a generic update method.

        Args:
            config:
        """
        assert self.__class__.allows_discrete_actions is not None and self.__class__.allows_continuous_actions is not None
        config.default(Model.default_config)

        self.discount = config.discount
        self.distributed = config.distributed
        self.session = None

        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(util.log_levels[config.log_level])

        if not config.distributed:
            assert not config.global_model and config.session is None
            tf.reset_default_graph()

        if config.distributed and not config.global_model:
            # Global and local model for asynchronous updates
            global_config = config.copy()
            global_config.optimizer = None
            global_config.global_model = True
            global_config.device = tf.train.replica_device_setter(1, worker_device=config.device, cluster=config.cluster_spec)
            self.global_model = self.__class__(config=global_config)
            self.global_timestep = self.global_model.global_timestep
            self.global_episode = self.global_model.episode
            self.global_variables = self.global_model.variables

        self.optimizer_args = None
        with tf.device(config.device):
            if config.distributed:
                if config.global_model:
                    self.global_timestep = tf.get_variable(name='timestep', dtype=tf.int32, initializer=0, trainable=False)
                    self.episode = tf.get_variable(name='episode', dtype=tf.int32, initializer=0, trainable=False)
                    scope_context = tf.variable_scope('global')
                else:
                    scope_context = tf.variable_scope('local')
                scope = scope_context.__enter__()

            self.create_tf_operations(config)

            if config.distributed:
                self.variables = tf.contrib.framework.get_variables(scope=scope)

            assert self.optimizer or (not config.distributed or config.global_model)
            if self.optimizer:
                if config.distributed and not config.global_model:
                    self.loss = tf.add_n(inputs=tf.losses.get_losses(scope=scope.name))
                    local_grads_and_vars = self.optimizer.compute_gradients(loss=self.loss, var_list=self.variables)
                    local_gradients = [grad for grad, var in local_grads_and_vars]
                    global_gradients = list(zip(local_gradients, self.global_model.variables))
                    self.update_local = tf.group(*(v1.assign(v2) for v1, v2 in zip(self.variables, self.global_model.variables)))
                    self.optimize = tf.group(
                        self.optimizer.apply_gradients(grads_and_vars=global_gradients),
                        self.update_local,
                        self.global_timestep.assign_add(tf.shape(self.reward)[0]))
                    self.increment_global_episode = self.global_episode.assign_add(tf.count_nonzero(input_tensor=self.terminal, dtype=tf.int32))
                else:
                    self.loss = tf.losses.get_total_loss()
                    for l in tf.losses.get_losses():
                        tf.summary.scalar('loss/{}'.format(l.name), l)
                    if self.optimizer_args is not None:
                        self.optimizer_args['loss'] = self.loss
                        self.optimize = self.optimizer.minimize(self.optimizer_args)
                    else:
                        self.optimize = self.optimizer.minimize(self.loss)
            if config.distributed:
                scope_context.__exit__(None, None, None)

        self.saver = tf.train.Saver(max_to_keep=1000)
        if config.tf_summary is not None:
            # create summary writer
            self.writer = tf.summary.FileWriter(config.tf_summary, graph=tf.get_default_graph())
            self._init_tf_writer(config.tf_summary_level, True)
            self._write_each_reward = True
        else:
            self.writer = None
            self._write_each_reward = False

        self.timestep = 0
        self.summary_interval = config.tf_summary_interval

        if not config.distributed:
            self.set_session(tf.Session())
            self.session.run(tf.global_variables_initializer())
            # tf.get_default_graph().finalize()

    def _init_tf_writer(self, tf_summary_level):
        # create a summary for total loss
        if hasattr(self, 'loss'):
            tf.summary.scalar('loss/total', self.loss)
        if hasattr(self, 'q_deltas'):
            tf.summary.histogram('loss/q-delta', self.q_deltas)

        self.last_summary_step = -float('inf')

        # create summaries based on summary level
        if tf_summary_level >= 2:  # trainable variables
            for v in tf.trainable_variables():
                tf.summary.histogram(v.name, v)

        # merge all summaries
        self.tf_summaries = tf.summary.merge_all()

        # create a separate summary for episode rewards
        if self._write_each_reward:
            self.tf_episode_reward = tf.placeholder(tf.float32, name='episode-reward-placeholder')
            self.episode_reward_summary = tf.summary.scalar('episode-reward', self.tf_episode_reward)

    def add_tf_writer(self, writer, tf_summary_level):
        self.writer = writer
        self._init_tf_writer(tf_summary_level)

    def create_tf_operations(self, config):
        """
        Creates generic TensorFlow operations and placeholders required for models.

        Args:
            config: Model configuration which must contain entries for states and actions.

        Returns:

        """
        self.action_taken = dict()
        self.internal_inputs = list()
        self.internal_outputs = list()
        self.internal_inits = list()

        # Placeholders
        with tf.variable_scope('placeholder'):
            # States
            self.state = dict()
            for name, state in config.states.items():
                self.state[name] = tf.placeholder(dtype=util.tf_dtype(state.type), shape=(None,) + tuple(state.shape), name=name)

            # Actions
            self.action = dict()
            self.discrete_actions = []
            self.continuous_actions = []
            for name, action in config.actions:
                if action.continuous:
                    if not self.__class__.allows_continuous_actions:
                        raise TensorForceError("Error: Model does not support continuous actions.")
                    self.action[name] = tf.placeholder(dtype=util.tf_dtype('float'), shape=(None,) + tuple(action.shape), name=name)
                else:
                    if not self.__class__.allows_discrete_actions:
                        raise TensorForceError("Error: Model does not support discrete actions.")
                    self.action[name] = tf.placeholder(dtype=util.tf_dtype('int'), shape=(None,) + tuple(action.shape), name=name)

            # Reward & terminal
            self.reward = tf.placeholder(dtype=tf.float32, shape=(None,), name='reward')
            self.terminal = tf.placeholder(dtype=tf.bool, shape=(None,), name='terminal')

            # Deterministic action flag
            self.deterministic = tf.placeholder(dtype=tf.bool, shape=(), name='deterministic')

        # Optimizer
        if config.optimizer is not None:
            with tf.variable_scope('optimization'):
                self.optimizer = Optimizer.from_config(
                    config=config.optimizer,
                    kwargs=dict(learning_rate=config.learning_rate)
                )
        else:
            self.optimizer = None

    def set_session(self, session):
        assert self.session is None
        self.session = session

    def reset(self):
        """
        Resets the internal state to the initial state.
        Returns: A list containing the internal_inits field.

        """
        return list(self.internal_inits)

    def get_action(self, state, internal, deterministic=False):
        self.timestep += 1
        fetches = {action: action_taken for action, action_taken in self.action_taken.items()}
        fetches.update({n: internal_output for n, internal_output in enumerate(self.internal_outputs)})

        feed_dict = {state_input: (state[name],) for name, state_input in self.state.items()}
        feed_dict.update({internal_input: (internal[n],) for n, internal_input in enumerate(self.internal_inputs)})
        feed_dict[self.deterministic] = deterministic

        fetched = self.session.run(fetches=fetches, feed_dict=feed_dict)

        action = {name: fetched[name][0] for name in self.action}
        internal = [fetched[n][0] for n in range(len(self.internal_outputs))]
        return action, internal

    def _maybe_append_tf_summaries(self, fetches):
        if self.should_write_summaries():
            return fetches + [self.tf_summaries]
        else:
            return fetches

    def update(self, batch):
        """Generic batch update operation for Q-learning and policy gradient algorithms.
         Takes a batch of experiences,

        Args:
            batch: Batch of experiences.

        Returns:

        """
        if self.optimizer is None:
            return

        fetches = [self.optimize, self.loss, self.loss_per_instance]
        feed_dict = self.update_feed_dict(batch=batch)

        if self.distributed:
            fetches.extend(self.increment_global_episode for terminal in batch['terminals'] if terminal)

        with SummarySessionWrapper(self, fetches, feed_dict) as session:
            returns = session.run()
        loss, loss_per_instance = returns[1:3]

        self.logger.debug('Computed update with loss = {}.'.format(loss))

        return loss, loss_per_instance

    def update_feed_dict(self, batch):
        feed_dict = {state: batch['states'][name] for name, state in self.state.items()}
        feed_dict.update({action: batch['actions'][name] for name, action in self.action.items()})
        feed_dict[self.reward] = batch['rewards']
        feed_dict[self.terminal] = batch['terminals']
        feed_dict.update({internal: batch['internals'][n] for n, internal in enumerate(self.internal_inputs)})
        return feed_dict

    def load_model(self, path):
        """
        Import model from path using tf.train.Saver.

        Args:
            path: Path to checkpoint

        Returns:

        """
        self.saver.restore(self.session, path)

    def save_model(self, path, use_global_step=True):
        """
        Export model using a tf.train.Saver. Optionally append current time step as to not
        overwrite previous checkpoint file. Set to 'false' to be able to load model
        from exact path it was saved to in case of restarting program.

        Args:
            path: Model export directory
            use_global_step: Whether to append the current timestep to the checkpoint path.

        Returns:

        """
        if use_global_step:
            self.saver.save(self.session, path, global_step=self.timestep)
        else:
            self.saver.save(self.session, path)

    def should_write_summaries(self):
        return self.writer is not None and self.timestep > self.last_summary_step + self.summary_interval

    def write_summaries(self, summaries):
        self.writer.add_summary(summaries, global_step=self.timestep)

    def write_episode_reward_summary(self, episode_reward):
        if self._write_each_reward and self.writer is not None:
            reward_summary = self.session.run(self.episode_reward_summary, feed_dict={self.tf_episode_reward: episode_reward})
            self.writer.add_summary(reward_summary, global_step=self.timestep)
