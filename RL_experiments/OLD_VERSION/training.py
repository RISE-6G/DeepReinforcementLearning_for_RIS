import dataclasses
import json
import os
from copy import deepcopy
from typing import Tuple, Callable, Union
import numpy as np
import matplotlib.pyplot as plt
import builtins
from dataclasses import dataclass, field
import tensorflow as tf
import tf_agents
from tf_agents import networks, bandits
from tf_agents.agents import TFAgent
from tf_agents.agents.dqn import dqn_agent
from tf_agents.bandits.agents import neural_linucb_agent
from tf_agents.drivers import dynamic_step_driver
from tf_agents.environments.tf_py_environment import TFPyEnvironment
from tf_agents.networks import sequential
from tf_agents.policies.policy_saver import PolicySaver
from tf_agents.policies.tf_policy import TFPolicy
from tf_agents.replay_buffers import tf_uniform_replay_buffer
from tf_agents.trajectories import trajectory
from tf_agents.utils.common import element_wise_squared_loss, element_wise_huber_loss
from tf_agents.trajectories import time_step as ts
from tf_agents.bandits.agents.neural_epsilon_greedy_agent import NeuralEpsilonGreedyAgent
from tf_agents.networks import network
from scipy.interpolate import make_interp_spline, BSpline
import seaborn as sns
from scipy.ndimage.filters import gaussian_filter1d
import pandas as pd



from tqdm import tqdm




# ------------------------------- EVALUATION FUNCTIONS ----------------------------- #


def compute_avg_return(environment, policy, num_timesteps=100):
    """
    :param environment: A  TfPyEnvironment instance
    :param policy: A TFPolicy in
    :param num_timesteps:
    :return: float
    """
    returns = []
    time_step = environment.reset()

    for ts_counter in range(num_timesteps):
        if  time_step.is_last():
            time_step = environment.reset()

        action_step = policy.action(time_step)
        time_step = environment.step(action_step.action)
        returns.append( float(time_step.reward.numpy()) )


    returns = np.array(returns)
    avg_return = float(np.mean(returns))
    std_return = float(np.std(returns))

    return avg_return, std_return


def moving_average(x, w):
    return np.convolve(x, np.ones(w), 'valid') / w




def evaluate_agent(agent_policy, environment, num_timesteps, name=None, print_=True):
    trained_agent_rewards = np.zeros(num_timesteps)
    time_step = environment.reset()
    for i in range(num_timesteps):
        action = agent_policy.action(time_step)
        time_step = environment.step(action)
        trained_agent_rewards[i] = float(time_step.reward)

    if print_:
        name = name if name is not None else 'Trained agent'
        print('\n\n{} policy average reward: {:2e} ± {:2e}\n'.format(
            name,
            trained_agent_rewards.mean(),
            trained_agent_rewards.std()
        ))

    return trained_agent_rewards.mean()


class RewardObserver:
    def __init__(self, num_iterations, log_interval):
        self.reward_values = np.zeros(num_iterations)
        self.it_cnt = 0
        self.num_iterations = num_iterations
        self.log_interval = log_interval

    def __call__(self, trajectory):
        curr_reward = float(trajectory.reward.numpy())
        if self.it_cnt < self.num_iterations:
            self.reward_values[self.it_cnt] = curr_reward
        else:
            print("\n#")

        self.it_cnt += 1
        if self.it_cnt % self.log_interval == 0:
            last_window_rewards = self.reward_values[self.it_cnt - self.log_interval:self.it_cnt]
            avg_reward = last_window_rewards.mean()
            tqdm.write('Iteration {}: Window avg reward: {}'.format(self.it_cnt,
                                                                    avg_reward))




# ---------------------------- Neural BANDIT TRAINING ----------------------------- #


@dataclass
class NeuralLinUCBParams:
    fc_layer_params : Tuple
    encoding_dim    : int
    num_iterations  : int
    steps_per_loop  : int
    batch_size      : int
    log_interval    : int
    eval_interval   : int
    learning_rate   : float
    num_eval_episodes: int
    encoding_network_num_train_steps : float
    epsilon_greedy  : float
    alpha           : float
    gamma           : float
    num_actions     : float
    gradient_clipping : float

    def __post_init__(self):
        pass


def initialize_Neural_Lin_UCB_agent(params: NeuralLinUCBParams, train_env: TFPyEnvironment):
    num_actions = int(train_env.action_spec().maximum) - int(train_env.action_spec().minimum) + 1
    params.num_actions = num_actions
    params.num_iterations *= params.num_actions
    params.encoding_network_num_train_steps = int(params.encoding_network_num_train_steps * params.num_iterations)

    encoding_network_layers = list(params.fc_layer_params)
    encoding_network_layers.append(params.encoding_dim)

    encoding_network = networks.encoding_network.EncodingNetwork(
        input_tensor_spec=train_env.observation_spec(),
        fc_layer_params=encoding_network_layers,
    )

    nlucb_agent = neural_linucb_agent.NeuralLinUCBAgent(
        time_step_spec=ts.time_step_spec(train_env.observation_spec()),
        action_spec=train_env.action_spec(),
        encoding_network=encoding_network,
        encoding_network_num_train_steps=params.encoding_network_num_train_steps,
        encoding_dim=params.encoding_dim,
        epsilon_greedy=params.epsilon_greedy,
        alpha=params.alpha,
        gamma=params.gamma,
        optimizer=tf.keras.optimizers.Adam(learning_rate=params.learning_rate),
        gradient_clipping=params.gradient_clipping

    )

    return nlucb_agent





@dataclass
class NeuralEpsilonGreedyParams:
    fc_layer_params : Tuple
    dropout_p       : float
    kernel_l2_reg   : float
    initialization_variance_scale : float
    num_iterations  : int
    steps_per_loop  : int
    batch_size      : int
    log_interval    : int
    eval_interval   : int
    learning_rate   : float
    num_eval_episodes: int
    epsilon_greedy  : float
    num_actions     : float
    gradient_clipping : float

    def __post_init__(self):
        pass




class RewardNet(network.Network):

    def __init__(self,
               input_tensor_spec,
               output_tensor_spec,
               num_actions,
               init_variance_scaling,
               l2_reg,
               fc_layer_params,
               dropout_p,):

        super(RewardNet, self).__init__(input_tensor_spec=input_tensor_spec,state_spec=(), name='RewardNet')

        initializer = lambda: tf.keras.initializers.VarianceScaling(scale=init_variance_scaling, mode='fan_in',                                                            distribution='truncated_normal')
        regularizer = lambda: tf.keras.regularizers.l2(l2_reg)


        self._output_tensor_spec = output_tensor_spec
        self._sub_layers = []


        reshapeLayer = tf.keras.layers.Reshape((-1,1,1))
        convLayer1   = tf.keras.layers.Conv2D(64, (5,1), data_format="channels_last")
        maxPool1     = tf.keras.layers.MaxPool2D((4,1))
        convLayer2   = tf.keras.layers.Conv2D(64, (5,1), data_format="channels_last")
        maxPool2     = tf.keras.layers.MaxPool2D((4, 1))
        flatten      = tf.keras.layers.Flatten()


        self._sub_layers = [reshapeLayer, convLayer1, maxPool1, convLayer2, maxPool2, flatten]

        for units in fc_layer_params:
            layer = tf.keras.layers.Dense(units,
                                          activation='relu',
                                          kernel_initializer = initializer(),
                                          kernel_regularizer=regularizer())
            self._sub_layers.append(layer)
            self._sub_layers.append(tf.keras.layers.Dropout(dropout_p))


        output_layer =  tf.keras.layers.Dense(num_actions,
                                              activation='linear',
                                              kernel_initializer = initializer(),
                                              kernel_regularizer=regularizer())
        self._sub_layers.append(output_layer)


    def call(self, observations, step_type=None, network_state=()):
        del step_type

        output = tf.cast(observations, dtype=tf.float32)
        for layer in self._sub_layers:
          output = layer(output)

        #actions = tf.reshape(output, [-1] + self._output_tensor_spec.shape.as_list())
        actions = output

        # Scale and shift actions to the correct range if necessary.
        return actions, network_state



def initialize_NeuralEpsilonGreedyAgent(params: NeuralEpsilonGreedyParams, train_env: TFPyEnvironment):
    num_actions = int(train_env.action_spec().maximum) - int(train_env.action_spec().minimum) + 1
    params.num_actions = num_actions
    params.num_iterations = int(params.num_iterations *  params.num_actions)


    reward_network = RewardNet(train_env.observation_spec(),
                               train_env.action_spec(),
                               num_actions,
                               params.initialization_variance_scale,
                               params.kernel_l2_reg,
                               params.fc_layer_params,
                               params.dropout_p)

    agent = NeuralEpsilonGreedyAgent(
        time_step_spec=ts.time_step_spec(train_env.observation_spec()),
        action_spec=train_env.action_spec(),
        reward_network=reward_network,
        optimizer=tf.keras.optimizers.Adam(learning_rate=params.learning_rate),
        epsilon=params.epsilon_greedy,
        gradient_clipping=params.gradient_clipping,
        name='NeuralEpsilonGreedy',
    )
    return agent








# ----------------------------- GENERAL BANDIT TRAINING -------------------------------------------

def train_bandit_agent(agent: TFAgent,
                       environment: TFPyEnvironment,
                       params: Union[NeuralLinUCBParams, NeuralEpsilonGreedyParams]):

    #reward_observer = RewardObserver(params.num_iterations, params.log_interval)


    log_dir = f"./logs/{agent.name}/RIS_elements_{params.num_actions}_iters_{params.num_iterations}/"
    tensorboard_log_dir = log_dir + "tensorboard-logs/"
    os.makedirs(log_dir, exist_ok=True)
    os.makedirs(tensorboard_log_dir, exist_ok=True)
    print(f"Tensorboard logs on: {tensorboard_log_dir}")
    writer = tf.summary.create_file_writer(tensorboard_log_dir)

    replay_buffer = tf_uniform_replay_buffer.TFUniformReplayBuffer(
        data_spec=agent.policy.trajectory_spec,
        batch_size=params.batch_size,
        max_length=params.steps_per_loop)

    #observers = [replay_buffer.add_batch, reward_observer]
    observers = [replay_buffer.add_batch]




    driver = dynamic_step_driver.DynamicStepDriver(
        env=environment,
        policy=agent.collect_policy,
        num_steps=params.steps_per_loop * params.batch_size,
        observers=observers)

    loss_infos = []
    rewards    = []
    eval_steps = []

    best_policy = None
    best_return = -np.inf
    try:
        for step in tqdm(range(params.num_iterations)):
            driver.run()
            loss_info = agent.train(replay_buffer.gather_all())
            replay_buffer.clear()
            loss_infos.append(loss_info.loss.numpy())
            tf.summary.scalar('Loss', loss_info.loss.numpy(), step=step)

            if step % params.eval_interval == 0:
                avg_return, std_return = compute_avg_return(environment, agent.policy, params.num_eval_episodes)
                tqdm.write('step = {0}: Average Return = {1:.4f} +/- {2:.3f}'.format(step, avg_return, std_return))
                rewards.append(avg_return)
                eval_steps.append(step)
                tf.summary.scalar('Average Reward', avg_return, step=step)
                writer.flush()

                if avg_return > best_return:
                    best_return, best_policy = avg_return, deepcopy(agent.policy)


        #PolicySaver(agent.policy).save(log_dir+'trained_policy')

    except KeyboardInterrupt:
        print('Training stopped by user...')

    return rewards, loss_infos, eval_steps, best_policy






# ---------------------- DQN TRAINING ---------------------------------- #


@dataclass()
class DQNParams:
    fc_layer_params            : Tuple
    num_iterations             : int
    initial_collect_steps      : int
    collect_steps_per_iteration: int
    replay_buffer_max_length   : int
    batch_size                 : int
    learning_rate              : float
    log_interval               : int
    eval_interval              : int
    epsilon_greedy             : float
    gradient_clipping          : float
    n_step_update              : int
    target_update_tau          : float
    target_update_period       : int
    gamma                      : float
    num_eval_episodes          : int
    num_actions                : int
    td_errors_loss_fn          : Callable = element_wise_squared_loss

    def __post_init__(self):
        #self.num_iterations *= self.num_actions
        pass

def _construct_Q_network(num_actions, fc_layer_params):
    def dense_layer(num_units):
          return tf.keras.layers.Dense(
              num_units,
              activation           = tf.keras.activations.relu,
              #kernel_regularizer   = tf.keras.regularizers.l1(10e-3),
              #activity_regularizer = tf.keras.regularizers.l2(.2),
              kernel_initializer   = tf.keras.initializers.VarianceScaling(scale=5.0, mode='fan_in',
                                                                           distribution='truncated_normal'),
          )


    dense_layers = [dense_layer(num_units) for num_units in fc_layer_params]
    q_values_layer = tf.keras.layers.Dense(
        num_actions,
        activation         = None,
        kernel_initializer = tf.keras.initializers.RandomUniform(minval=-0.03, maxval=0.03),
        bias_initializer   = tf.keras.initializers.Constant(-0.2),
        )

    return sequential.Sequential(dense_layers + [q_values_layer])


def initialize_DQN_agent(params: DQNParams, train_env: TFPyEnvironment):
    num_actions = int(train_env.action_spec().maximum) - int(train_env.action_spec().minimum) + 1
    params.num_actions = num_actions
    params.num_iterations = int(params.num_iterations *  params.num_actions)


    q_net        = _construct_Q_network(num_actions, params.fc_layer_params)
    target_q_net = _construct_Q_network(num_actions, params.fc_layer_params)
    optimizer    = tf.keras.optimizers.Adam(learning_rate=params.learning_rate)

    train_step_counter = tf.Variable(0)

    agent = dqn_agent.DqnAgent(
        time_step_spec=train_env.time_step_spec(),
        action_spec=train_env.action_spec(),
        q_network=q_net,
        target_q_network=target_q_net,
        optimizer=optimizer,
        train_step_counter=train_step_counter,
        td_errors_loss_fn=params.td_errors_loss_fn,
        epsilon_greedy=params.epsilon_greedy,
        gradient_clipping=params.gradient_clipping,
        n_step_update=params.n_step_update,
        target_update_tau=params.target_update_tau,
        target_update_period=params.target_update_period,
        gamma=params.gamma,

    )

    agent.initialize()

    return agent




def _collect_step(environment, policy, buffer):
    time_step      = environment.current_time_step()
    action_step    = policy.action(time_step)
    next_time_step = environment.step(action_step.action)
    traj           = trajectory.from_transition(time_step, action_step, next_time_step)
    buffer.add_batch(traj)

def _collect_data(env, policy, buffer, steps):
    for _ in range(steps):
        _collect_step(env, policy, buffer)

def train_DQN_agent(agent: dqn_agent.DqnAgent,
                    train_env: TFPyEnvironment,
                    params: DQNParams,
                    random_policy: TFPolicy):

    replay_buffer = tf_uniform_replay_buffer.TFUniformReplayBuffer(
        data_spec=agent.collect_data_spec,
        batch_size=train_env.batch_size,
        max_length=params.replay_buffer_max_length)

    _collect_data(train_env, random_policy, replay_buffer, params.initial_collect_steps)

    dataset = replay_buffer.as_dataset(
        num_parallel_calls=3,
        sample_batch_size=params.batch_size,
        num_steps=2).prefetch(3)

    iterator = iter(dataset)

    # (Optional) Optimize by wrapping some of the code in a graph using TF function.
    agent.train = tf_agents.utils.common.function(agent.train)

    # Reset the train step
    agent.train_step_counter.assign(0)

    # Evaluate the agent's policy once before training.
    avg_return, _ = compute_avg_return(train_env, agent.policy, params.num_eval_episodes)
    returns          = [avg_return]
    train_losses     = []
    eval_steps       = [0]

    print('Starting training')

    try:
        for iter_cnt in tqdm(range(params.num_iterations)):
            _collect_data(train_env, agent.collect_policy, replay_buffer, params.collect_steps_per_iteration)
            experience, unused_info = next(iterator)
            train_loss = agent.train(experience).loss
            step = agent.train_step_counter.numpy()

            train_losses.append(train_loss)

            if step % params.log_interval == 0:
                print('step = {0}: loss = {1}'.format(step, train_loss))

            if step % params.eval_interval == 0:
                avg_return, std_return = compute_avg_return(train_env, agent.policy, params.num_eval_episodes)
                tqdm.write('step = {0}: Average Return = {1:.4f} +/- {2:.3f}'.format(step, avg_return, std_return))
                returns.append(avg_return)

    except KeyboardInterrupt:
        print('Training aborted by user...')
        num_iterations = iter_cnt


    return returns, train_losses, eval_steps










# -------------- PLOTTING FUNCTIONS ---------------------



def plot_loss(loss_values, agent_name, scale='linear', figsize=(16,9), smooth_sigma=None):
    plt.figure(figsize=figsize)
    x = np.arange(1, len(loss_values)+1)
    plt.plot(x, loss_values, label='original values')

    if smooth_sigma is not None:
        ysmoothed = gaussian_filter1d(loss_values, sigma=smooth_sigma)
        plt.plot(x, ysmoothed, label='smoothed')
        plt.legend()

    plt.xlabel('Iterations')
    plt.ylabel('Train loss')
    plt.yscale(scale)
    plt.title(f'{agent_name} training loss')
    plt.show()


def plot_training_performance(reward_values, iteration_timesteps, name=None, random_avg_reward=None, optimal_avg_reward=None, smooth_sigma=None):
    sns.set_theme()

    name = name if name is not None else 'Trained agent'


    plt.plot(iteration_timesteps, reward_values, alpha=.7, label=name)

    if random_avg_reward is not None:
        plt.hlines([random_avg_reward], 0, iteration_timesteps[-1], color='grey', ls=':', label='random policy')

    if optimal_avg_reward is not None:
        plt.hlines([optimal_avg_reward], 0, iteration_timesteps[-1], color='k', ls='--', label='optimal policy')


    if smooth_sigma is not None:
        ysmoothed = gaussian_filter1d(reward_values, sigma=smooth_sigma)
        plt.plot(iteration_timesteps, ysmoothed, label=f'{name} (smoothed)')

    plt.legend()

    plt.ylabel('Reward')
    plt.xlabel('Number of Iterations')
    plt.show(block=False)





def save_results(agent_name            : str,
                 setupParams           : dict,
                 agentParams           : dict,
                 reward_list           : list,
                 eval_steps            : list,
                 results_dict          : dict,
                 setup_dirname_params  : str,
                 agent_dirname_params  : str,
                 results_rootdir        = './results/',
                 ):

    def to_format_string(s):
        out = ''
        for variable in s.split(','):
            out += "_" + variable + "_{" + variable +"}"
        return out

    def generate_dirname(dirname_params, values_dict, prefix=''):
        fstring = to_format_string(dirname_params)
        dirname = fstring.format(**values_dict)
        if prefix:
            dirname = prefix + "_" + dirname
        return dirname + "/"

    setup_dirname = generate_dirname(setup_dirname_params, setupParams, prefix='setup')
    agent_dirname = generate_dirname(agent_dirname_params, agentParams, prefix=agent_name)

    all_dirs = os.path.join(results_rootdir, setup_dirname, agent_dirname)
    os.makedirs(all_dirs, exist_ok=True)

    with open(os.path.join(results_rootdir, setup_dirname, 'setup.json'), 'w') as fout:
        fout.write(json.dumps(setupParams, indent=4), )

    with open(os.path.join(results_rootdir, setup_dirname, agent_dirname, 'agent_params.json'), 'w') as fout:
        fout.write(json.dumps(agentParams, indent=4))

    with open(os.path.join(results_rootdir, setup_dirname, agent_dirname, 'agent_performance.json'), 'w') as fout:
        fout.write(json.dumps(results_dict, indent=4))

    with open(os.path.join(results_rootdir, setup_dirname, agent_dirname, 'agent_training.csv'), 'w') as fout:
        pd.DataFrame({
            'iteration' : eval_steps,
            'reward'    : reward_list,
        }).to_csv(fout, index=False)






