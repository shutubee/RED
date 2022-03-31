
import sys
import os
import argparse
import importlib.util

IMPORT_PATH = os.path.join(os.path.join(os.path.dirname(os.path.abspath(__file__))),'agents')
sys.path.append(IMPORT_PATH)
IMPORT_PATH = os.path.join(os.path.join(os.path.dirname(os.path.abspath(__file__))), 'environments')
sys.path.append(IMPORT_PATH)


import math
from casadi import *
import numpy as np
import matplotlib.pyplot as plt
from OED_env import *
from continuous_agents import *

import time


import tensorflow as tf

import multiprocessing
import json





def run_RT3D(xdot, param_path):
    # setup
    n_cores = multiprocessing.cpu_count()
    params = json.load(open(param_path))
    n_episodes, skip, y0, actual_params, input_bounds, n_controlled_inputs, num_inputs, dt, lb, ub, N_control_intervals, control_interval_time, n_observed_variables, prior, normaliser = \
        [params[k] for k in params.keys()]
    actual_params = DM(actual_params)
    normaliser = np.array(normaliser)
    n_params = actual_params.size()[0]
    n_system_variables = len(y0)
    n_FIM_elements = sum(range(n_params + 1))
    n_tot = n_system_variables + n_params * n_system_variables + n_FIM_elements
    param_guesses = actual_params
    physical_devices = tf.config.list_physical_devices('GPU')
    try:
        tf.config.experimental.set_memory_growth(physical_devices[0], True)
    except:
        pass
    save_path = './results/'

    # agent setup
    pol_learning_rate = 0.00005
    hidden_layer_size = [[64, 64], [128, 128]]
    pol_layer_sizes = [n_observed_variables + 1, n_observed_variables + 1 + n_controlled_inputs, hidden_layer_size[0],
                       hidden_layer_size[1], n_controlled_inputs]
    val_layer_sizes = [n_observed_variables + 1 + n_controlled_inputs, n_observed_variables + 1 + n_controlled_inputs,
                       hidden_layer_size[0], hidden_layer_size[1], 1]
    agent = DDPG_agent(val_layer_sizes=val_layer_sizes, pol_layer_sizes=pol_layer_sizes, policy_act=tf.nn.sigmoid,
                       val_learning_rate=0.0001, pol_learning_rate=pol_learning_rate)  # , pol_learning_rate=0.0001)
    agent.batch_size = int(N_control_intervals * skip)
    agent.max_length = 11
    agent.mem_size = 500000000
    agent.std = 0.1
    agent.noise_bounds = [-0.25, 0.25]
    agent.action_bounds = [0, 1]
    policy_delay = 2
    update_count = 0
    max_std = 1  # for exploring
    explore_rate = max_std
    alpha = 1
    all_returns = []
    all_test_returns = []

    # env setup
    args = y0, xdot, param_guesses, actual_params, n_observed_variables, n_controlled_inputs, num_inputs, input_bounds, dt, control_interval_time, normaliser
    env = OED_env(*args)
    env.mapped_trajectory_solver = env.CI_solver.map(skip, "thread", n_cores)

    for episode in range(int(n_episodes // skip)):  # training loop
        actual_params = np.random.uniform(low=lb, high=ub, size=(skip, 3))  # sample from uniform distribution
        env.param_guesses = DM(actual_params)
        states = [env.get_initial_RL_state_parallel() for i in range(skip)]
        e_returns = [0 for _ in range(skip)]
        e_actions = []
        e_exploit_flags = []
        e_rewards = [[] for _ in range(skip)]
        e_us = [[] for _ in range(skip)]
        trajectories = [[] for _ in range(skip)]
        sequences = [[[0] * pol_layer_sizes[1]] for _ in range(skip)]
        env.reset()
        env.param_guesses = DM(actual_params)
        env.logdetFIMs = [[] for _ in range(skip)]
        env.detFIMs = [[] for _ in range(skip)]

        for e in range(0, N_control_intervals):  # run an episode
            inputs = [states, sequences]
            if episode < 1000 // skip:
                actions = agent.get_actions(inputs, explore_rate=1, test_episode=True, recurrent=True)
            else:
                actions = agent.get_actions(inputs, explore_rate=explore_rate, test_episode=True, recurrent=True)

            e_actions.append(actions)
            outputs = env.map_parallel_step(np.array(actions).T, actual_params, continuous=True)
            next_states = []

            for i, o in enumerate(outputs):  # extract outputs from parallel experiments
                next_state, reward, done, _, u = o
                e_us[i].append(u)
                next_states.append(next_state)
                state = states[i]
                action = actions[i]

                if e == N_control_intervals - 1 or np.all(np.abs(next_state) >= 1) or math.isnan(np.sum(next_state)):
                    done = True

                transition = (state, action, reward, next_state, done)
                trajectories[i].append(transition)
                sequences[i].append(np.concatenate((state, action)))
                if reward != -1:  # dont include the unstable trajectories as they override the true return
                    e_rewards[i].append(reward)
                    e_returns[i] += reward
            states = next_states

        for trajectory in trajectories:
            if np.all([np.all(np.abs(trajectory[i][0]) <= 1) for i in range(len(trajectory))]) and not math.isnan(
                    np.sum(trajectory[-1][0])):  # check for instability
                agent.memory.append(trajectory)

        if episode > 1000 // skip:  # train agent
            print('training', update_count)
            t = time.time()
            for _ in range(skip):
                update_count += 1
                policy = update_count % policy_delay == 0

                agent.Q_update(policy=policy, fitted=False, recurrent=True)
            print('fitting time', time.time() - t)

        explore_rate = agent.get_rate(episode, 0, 1, n_episodes / (11 * skip)) * max_std

        all_returns.extend(e_returns)
        print()
        print('EPISODE: ', episode, episode * skip)

        print('av return: ', np.mean(all_returns[-skip:]))
        print()

    # plot and save results
    np.save(save_path + 'all_returns.npy', np.array(all_returns))
    np.save(save_path + 'actions.npy', np.array(agent.actions))
    agent.save_network(save_path)

    t = np.arange(N_control_intervals) * int(control_interval_time)

    plt.plot(all_test_returns)
    plt.figure()
    plt.plot(all_returns)
    plt.show()


parser = argparse.ArgumentParser(description='Run the RT3D algorithm for OED')

parser.add_argument('xdot_path', type=str, help='the filepath with the function that defines the differential equations')
parser.add_argument('param_path', type=str, help='the filepath for the learning parameters json file')



if __name__ == '__main__':
    args = parser.parse_args()

    # extract args
    xdot_path = args.xdot_path
    param_path = args.param_path
    print(xdot_path)

    #import the xdot function
    spec = importlib.util.spec_from_file_location('xdot', xdot_path)
    xdot_mod = importlib.util.module_from_spec(spec)
    sys.modules['xdot'] = xdot_mod
    spec.loader.exec_module(xdot_mod)


    run_RT3D(xdot_mod.xdot, param_path)






