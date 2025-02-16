# -*- coding: utf-8 -*-

import copy
import numpy as np
from chainer import cuda, FunctionSet, Variable, optimizers, serializers
import chainer.functions as F


class QNet:
    # Hyper-Parameters
    gamma = 0.99  # 報酬の割引率
    replay_size = 32  # Replay (batch) size
    target_model_update_freq = 10**2  # Target update frequancy. original: 10^4
    data_size = 10**4  # Data size of history. original: 10^6
    hist_size = 4 #original: 4
    save_model_freq = 10**3 # モデルを保存する頻度

    def __init__(self, use_gpu, enable_controller, dim):
        self.use_gpu = use_gpu
        self.num_of_actions = len(enable_controller)
        self.enable_controller = enable_controller
        self.dim = dim

        print("Initializing Q-Network...")
        print("Input Dim of Q-Network : "),
        print(self.dim*self.hist_size)


        hidden_dim = 256

        self.model = FunctionSet(
            l4=F.Linear(self.dim*self.hist_size, hidden_dim,wscale=np.sqrt(2)),
            l5=F.Linear(hidden_dim,hidden_dim,wscale=np.sqrt(2)),
            q_value=F.Linear(hidden_dim, self.num_of_actions,
                            initialW=np.zeros((self.num_of_actions, hidden_dim),
                            dtype=np.float32))
        )

        if self.use_gpu >= 0:
            self.model.to_gpu()

        self.model_target = copy.deepcopy(self.model)

        self.optimizer = optimizers.RMSpropGraves(lr=0.00025, alpha=0.95, momentum=0.95, eps=0.0001)
        self.optimizer.setup(self.model.collect_parameters())

        # History Data :  D=[s, a, r, s_dash, end_episode_flag]
        self.d = [np.zeros((self.data_size, self.hist_size, self.dim),
                    dtype=np.uint8),
                  np.zeros(self.data_size, dtype=np.uint8),
                  np.zeros((self.data_size, 1), dtype=np.int8),
                  np.zeros((self.data_size, self.hist_size, self.dim),
                    dtype=np.uint8),
                  np.zeros((self.data_size, 1), dtype=np.bool)]

    def forward(self, state, action, reward, state_dash, episode_end):
        num_of_batch = state.shape[0]
        s = Variable(state)
        s_dash = Variable(state_dash)

        q = self.q_func(s)  # Get Q-value

        # Generate Target Signals
        tmp = self.q_func_target(s_dash)  # Q(s',*)
        if self.use_gpu >= 0:
            tmp = list(map(np.max, tmp.data.get()))  # max_a Q(s',a)
        else:
            tmp = list(map(np.max, tmp.data))  # max_a Q(s',a)

        max_q_dash = np.asanyarray(tmp, dtype=np.float32)
        if self.use_gpu >= 0:
            target = np.asanyarray(q.data.get(), dtype=np.float32)
        else:
            # make new array
            target = np.array(q.data, dtype=np.float32)

        for i in xrange(num_of_batch):
            if not episode_end[i][0]:
                tmp_ = reward[i] + self.gamma * max_q_dash[i]
            else:
                tmp_ = reward[i]

            action_index = self.action_to_index(action[i])
            target[i, action_index] = tmp_

        # TD-error clipping
        if self.use_gpu >= 0:
            target = cuda.to_gpu(target)
        td = Variable(target) - q  # TD error
        td_tmp = td.data + 1000.0 * (abs(td.data) <= 1)  # Avoid zero division
        td_clip = td * (abs(td.data) <= 1) + td/abs(td_tmp) * (abs(td.data) > 1)

        zero_val = np.zeros((self.replay_size, self.num_of_actions), dtype=np.float32)
        if self.use_gpu >= 0:
            zero_val = cuda.to_gpu(zero_val)
        zero_val = Variable(zero_val)
        loss = F.mean_squared_error(td_clip, zero_val)
        return loss, q

    def stock_experience(self, time,state, action, reward,
                        state_dash,episode_end_flag):

        data_index = time % self.data_size #timeを引数に入れることでqueueを実現
        if episode_end_flag is True:
            self.d[0][data_index] = state
            self.d[1][data_index] = action
            self.d[2][data_index] = reward
        else:
            self.d[0][data_index] = state
            self.d[1][data_index] = action
            self.d[2][data_index] = reward
            self.d[3][data_index] = state_dash
        self.d[4][data_index] = episode_end_flag
        print "Stock Exprience Episode End:%r Reward:%.3f"%(episode_end_flag,reward)

    def experience_replay(self, time):
        # 例 : np.random.randint(0,100,(5,5))  0〜99 の整数で5x5の行列を生成
        if time < self.data_size: #during the first sweep of the History
            replay_index = np.random.randint(0, time, (self.replay_size, 1))
        else:
            replay_index = np.random.randint(0, self.data_size, (self.replay_size, 1))

        s_replay = np.ndarray(shape=(self.replay_size, self.hist_size, self.dim), dtype=np.float32)
        a_replay = np.ndarray(shape=(self.replay_size, 1), dtype=np.uint8)
        r_replay = np.ndarray(shape=(self.replay_size, 1), dtype=np.float32)
        s_dash_replay = np.ndarray(shape=(self.replay_size, self.hist_size, self.dim), dtype=np.float32)
        episode_end_replay = np.ndarray(shape=(self.replay_size, 1), dtype=np.bool)
        for i in xrange(self.replay_size):
            s_replay[i] = np.asarray(self.d[0][replay_index[i]], dtype=np.float32)
            a_replay[i] = self.d[1][replay_index[i]]
            r_replay[i] = self.d[2][replay_index[i]]
            s_dash_replay[i] = np.array(self.d[3][replay_index[i]], dtype=np.float32)
            episode_end_replay[i] = self.d[4][replay_index[i]]

        if self.use_gpu >= 0:
            s_replay = cuda.to_gpu(s_replay)
            s_dash_replay = cuda.to_gpu(s_dash_replay)

        # Gradient-based update
        self.optimizer.zero_grads()
        loss, _ = self.forward(s_replay, a_replay, r_replay, s_dash_replay, episode_end_replay)
        loss.backward()
        self.optimizer.update()
        print "Replay Finish"

    def q_func(self, state):
        h4 = F.relu(self.model.l4(state / 255.0))
        h5 = F.relu(self.model.l5(h4))
        q = self.model.q_value(h5)
        return q

    def q_func_target(self, state):
        h4 = F.relu(self.model_target.l4(state / 255.0))
        h5 = F.relu(self.model_target.l5(h4))
        q = self.model_target.q_value(h5)
        return q

    def e_greedy(self, state, epsilon):
        s = Variable(state)
        q = self.q_func(s)
        q = q.data

        if np.random.rand() < epsilon:
            index_action = np.random.randint(0, self.num_of_actions)
            print(" Random"),
        else:
            if self.use_gpu >= 0:
                index_action = np.argmax(q.get())
            else:
                index_action = np.argmax(q)
        return self.index_to_action(index_action), q

    def target_model_update(self):
        self.model_target = copy.deepcopy(self.model)

    def index_to_action(self, index_of_action):
        return self.enable_controller[index_of_action]

    def action_to_index(self, action):
        return self.enable_controller.index(action)

    def save_model(self,folder,time):
        try:
            model_path = "./%s/%dmodel"%(folder,time)
            serializers.save_npz(model_path,self.model)
        except:
            import traceback
            import sys
            traceback.print_exc()
            sys.exit()
        print "model is saved!!(Model_Path=%s)"%(model_path)
        print "----------------------------------------------"


    def load_model(self,folder,model_num):
        try:
            model_path = "./%s/%dmodel"%(folder,model_num)
            serializers.load_npz(model_path,self.model)
        except:
            import traceback
            import sys
            traceback.print_exc()
            sys.exit()

        print "model load is done!!(Model_Path=%s)"%(model_path)
        print "----------------------------------------------"
        self.model_target = copy.deepcopy(self.model)
