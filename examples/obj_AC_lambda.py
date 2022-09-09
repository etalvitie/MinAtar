################################################################################################################
# Authors:                                                                                                     #
# Kenny Young (kjyoung@ualberta.ca)                                                                            #
# Tian Tian (ttian@ualberta.ca)                                                                                #
#                                                                                                              #
# python3 AC_lambda.py -g <game>                                                                               #
#   -o, --output <directory/file name prefix>                                                                  #
#   -v, --verbose: outputs the average returns every 1000 episodes                                             #
#   -l, --loadfile <directory/file name of the saved model>                                                    #
#   -a, --alpha <number>: custom step-size parameter                                                           #
#   -s, --save: save model data every 1000 episodes                                                            #                                                              #
#                                                                                                              #
# References used for this implementation:                                                                     #
#   https://pytorch.org/docs/stable/nn.html#                                                                   #
#   https://pytorch.org/docs/stable/torch.html                                                                 #
#   https://pytorch.org/tutorials/intermediate/reinforcement_q_learning.html                                   #
################################################################################################################

import torch
import torch.nn as nn
import torch.nn.functional as f
import pytorch_lightning as pl
import time
from tqdm import tqdm

import numpy, argparse, logging, os
import numpy as np

from collections import namedtuple
from environment import Environment
from velenvironment import Velenvironment


#####################################################################################################################
# Constants
#
#####################################################################################################################
NUM_FRAMES = 5000000
ALPHA = 0.00048828125
LAMBDA = 0.8
GAMMA = 0.99
BETA = 0.01
GAMMA_RMS = 0.999
EPS_RMS = 0.0001
MIN_DENOM = 0.0001

dSiLU = lambda x: torch.sigmoid(x)*(1+x*(1-torch.sigmoid(x)))
SiLU = lambda x: x*torch.sigmoid(x)

# device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
device = torch.device("cpu")

#####################################################################################################################
# ACNetwork
#
# Setup the AC-network with one hidden 2D conv with variable number of input channels. We use 16 filters, a quarter of
# the original DQN paper of 64. One hidden fully connected linear layer with a quarter of the original DQN paper of
# 512 rectified units. Finally, we use one output layer which is a fully connected softmax layer with a single output 
# for each valid action for the policy network, and another output which is a fully connected linear layer, with a
# single output for the state value. 
#
#####################################################################################################################
class ACNetwork(pl.LightningModule, nn.Module):
    def __init__(self, in_channels, num_actions, hidden_dim=128):

        super(ACNetwork, self).__init__()
        self.save_hyperparameters()

        self.dropout = nn.Dropout(p=0.2)

        # Embedding layers
        self.encoder = nn.Sequential(
            nn.Linear(in_features=in_channels, out_features=int(hidden_dim/4)),
            nn.ReLU(),
            self.dropout,
            nn.Linear(int(hidden_dim/4), int(hidden_dim/2)),
            nn.ReLU(),
            self.dropout,
            nn.Linear(int(hidden_dim/2), hidden_dim),
            nn.ReLU(),
            self.dropout,
        )

        # Output heads

        self.decoder = nn.Sequential(
            nn.Linear(int(hidden_dim), int(hidden_dim / 2)),
            nn.ReLU(),
            self.dropout,
            nn.Linear(int(hidden_dim / 2), num_actions)
        )
        
        self.value = nn.Linear(in_features=128, out_features=1)

    def forward(self, s, debug=False):
        """
        Input size: [BATCH_SIZE, N, M]
        """
        # batch size
        # bs = emb_objs.shape[1]

        # Calculate the object embedding
        emb_objs = self.encoder(s)
        emb_objs_vector, _ = torch.max(emb_objs, dim=1)
        policy = self.decoder(emb_objs_vector)
        # reward = self.decoder(emb_objs)
        # print("rewards: ", reward)
        # finally project transformer outputs to class labels and bounding boxes
        return f.softmax(policy, dim=1), self.value(emb_objs_vector)

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=1e-3)
        return optimizer

    def training_step(self, train_batch, batch_idx):
        loss = self.loss_fn(train_batch)
        self.log('train_loss', loss)
        return loss

    def validation_step(self, val_batch, batch_idx):
        loss = self.loss_fn(val_batch)
        self.log('train_loss', loss)

transition = namedtuple('transition', 'state, last_state, action, reward, is_terminal')

#####################################################################################################################
# get_state
#
# Converts the state given by the environment to a tensor of size (in_channel, 10, 10), and then
# unsqueeze to expand along the 0th dimension so the function returns a tensor of size (1, in_channel, 10, 10).
#
# Input:
#   s: current state as numpy array
#
# Output: current state as tensor, permuted to match expected dimensions
#
#####################################################################################################################
def get_state(s):
    return (torch.tensor(s, device=device).permute(2, 0, 1)).unsqueeze(0).float()

def get_cont_state(game, cont_s, max_obj=60):
    """
    Return the continuous state of the environment as a torch array.
    :param cont_s: Continuous state.
    :return: Torch tensor of shape [M*(2+N)]. M is the number of objects. N is the number of categories.
    """
    if game == "space_invaders":
        max_obj = 60
    N = len(cont_s)
    obj_len = len(cont_s[0][0])

    # Collect all the states
    cont_state = []
    for i in range(N):
        for obj in cont_s[i]:
            cont_state.append(torch.tensor(obj, device=device))

    # Convert into one torch tensor
    cont_state = torch.vstack(cont_state)

    # Zero pad to the maximum allowed dimension
    size_pad = max_obj - cont_state.shape[0]
    pad = torch.zeros((size_pad, obj_len), device=device)
    cont_state = torch.cat([cont_state, pad])

    # Unsqueeze for the batch dimension
    cont_state = cont_state.unsqueeze(0)
    
    return cont_state

#####################################################################################################################
# world_dynamics
#
# Responsible for world dynamics. It generates next state and reward after taking an action according to the behavior
# policy. The behavior policy is specified by the policy network output. Reward should be casted to float, otherwise
# it is LongTensor, which is used for indexing.
#
# Inputs:
#   s: current state
#   env: environment of the game
#   network: combined policy and value netork, an instance of ACNetwork
#
# Output: next state, action, reward, is_terminated
#
#####################################################################################################################
def world_dynamics(s, env, network):
    # network(s)[0] specifies the policy network, which we use to draw an action according to a multinomial 
    # distribution over axis 1, (axis 0 iterates over samples, and is unused in this case. torch._no_grad() 
    # avoids tracking history in autograd.
    with torch.no_grad():
        action = torch.multinomial(network(s)[0],1)[0]

    # Act according to the action and observe the transition and reward
    reward, terminated = env.act(action)

    # Obtain s_prime
    s_prime = get_cont_state(env.name, env.continuous_state())

    return s_prime, action, torch.tensor([[reward]], device=device).float(), torch.tensor([[terminated]], device=device)

#####################################################################################################################
# train
#
# This is where learning happens. More specifically, this function updates the weights of the policy/value network.
#
# Inputs:
#   sample: a single transition
#   traces: an instance of QNetwork
#   grads: a list of tensors, one for each network parameter. Used as temporary storage of computed gradient
#   network: an instance of ACNetwork, to be trained
#   alpha: learning rate for actor-critic update
#
#####################################################################################################################
def train(sample, traces, grads, MSGs, network, alpha, time_step):
    # states, next_states: (1, in_channel, 10, 10) - inline with pytorch NCHW format
    # actions, rewards, is_terminal: (1, 1)
    last_state = sample.last_state
    state = sample.state
    action = sample.action
    reward = sample.reward
    is_terminal = sample.is_terminal

    pi, V_curr = network(state)

    # Compute the targets
    trace_potential = V_curr+0.5*torch.log(pi[0,action]+MIN_DENOM)
    entropy = -torch.sum(torch.log(pi+MIN_DENOM)*pi)

    network.zero_grad()
    trace_potential.backward(retain_graph=True)

    with torch.no_grad():
        for param, grad in zip(network.parameters(), grads):
            grad.data.copy_(param.grad)

    # Update parameters except for on the first observation
    if(last_state is not None):
        network.zero_grad()
        entropy.backward()
        with torch.no_grad():
            V_last = network(last_state)[1]
            delta = GAMMA*(0 if is_terminal else V_curr)+reward-V_last

            # Update uses RMSProp with initialization debiasing
            for param, trace, MSG in zip(network.parameters(), traces, MSGs):
                grad = trace*delta[0]+BETA*param.grad
                MSG.copy_(GAMMA_RMS*MSG+(1-GAMMA_RMS)*grad*grad)
                param.copy_(param+alpha*grad/(torch.sqrt(MSG/(1-GAMMA_RMS**(time_step+1))+EPS_RMS)))

    # Always update trace
    with torch.no_grad():
        for grad, trace in zip(grads, traces):
            trace.copy_(LAMBDA*GAMMA*trace+grad)



#####################################################################################################################
# AC_lambda
#
# Online actor-critic algorithm with eligibiltiy traces but with no experience replay.
#
# Inputs:
#   env: environment of the game
#   replay_off: disable the replay buffer and train on each state transition
#   target_off: disable target network
#   output_file_name: directory and file name prefix to output data and network weights, file saved as 
#       <output_file_name>_data_and_weights
#   store_intermediate_result: a boolean, if set to true will store checkpoint data every 1000 episodes
#       to a file named <output_file_name>_checkpoint
#   load_path: directory of the file plus the file name of the saved model
#   alpha: step-size for use in actor-critic update
#
#####################################################################################################################
def AC_lambda(env, output_file_name, store_intermediate_result=False, load_path=None, alpha=ALPHA):
    torch.set_num_threads(1)
    # Get channels and number of actions specific to each game
    length = len(env.continuous_state()[0][0])
    in_channels = length #change
    num_actions = env.num_actions()

    # Instantiate networks, optimizer, loss and buffer
    network = ACNetwork(in_channels, num_actions).to(device)

    # Eligibility traces are stored here
    traces = [torch.zeros(x.size(), dtype=torch.float32, device=device) for x in network.parameters()]

    # Space allocated to store gradients used in training
    grads = [torch.zeros(x.size(), dtype=torch.float32, device=device) for x in network.parameters()]

    # Running average of mean squared gradient for use in RMSProp
    MSG = [torch.zeros(x.size(), dtype=torch.float32, device=device) for x in network.parameters()]

    # Set initial values
    e = 0
    t = 0
    avg_return = 0.0
    returns = []
    frame_stamps= []

    # Load model and optimizer if load_path is not None
    if load_path is not None and isinstance(load_path, str):
        checkpoint = torch.load(load_path)
        network.load_state_dict(checkpoint['network_state_dict'])
        e = checkpoint['episode']
        t = checkpoint['frame']
        avg_return = checkpoint['avg_return']
        returns = checkpoint['returns']
        frame_stamps = checkpoint['frame_stamps']

        # Set to training mode
        network.train()

    # Start the simulation
    # Train for a number of frames
    t_start = time.time()
    for t in tqdm(range(NUM_FRAMES)):
        # Initialize the return for every episode (we should see this eventually increase)
        G = 0.0

        # Initialize the environment and start state
        env.reset()
        s = get_cont_state(env.name, env.continuous_state())
        is_terminated = False
        s_last = None
        r_last = None
        term_last = None
        while(not is_terminated) and t < NUM_FRAMES:
            # Generate data
            s_prime, action, reward, is_terminated = world_dynamics(s, env, network)

            sample = transition(s, s_last, action, r_last, term_last)

            train(sample, traces, grads, MSG, network, alpha, t)

            G += reward.item()

            t += 1

            # Continue the process
            s_last = s
            r_last = reward
            term_last = is_terminated
            s = s_prime

        # Increment the episodes
        e += 1
        sample = transition(s, s_last, action, r_last, term_last)
        train(sample, traces, grads, MSG, network, alpha, t)

        # Clear elligibility traces after each episode
        for trace in traces:
            trace.zero_()


        # Save the return for each episode
        returns.append(G)
        frame_stamps.append(t)

        # Logging exponentiated return only when verbose is turned on and only at 1000 episode intervals
        avg_return = 0.99 * avg_return + 0.01 * G
        if e % 1000 == 0:
            logging.info("Episode " + str(e) + " | Return: " + str(G) + " | Avg return: " +
                         str(numpy.around(avg_return, 2)) + " | Frame: " + str(t)+" | Time per frame: " +
                         str((time.time()-t_start)/t) )
            f = open(f"{output_file_name}.txt", "a")
            f.write("Episode " + str(e) + " | Return: " + str(G) + " | Avg return: " +
                         str(np.around(avg_return, 2)) + " | Frame: " + str(t)+" | Time per frame: " +str((time.time()-t_start)/t) + "\n" )
            f.close()

        # Save model data and other intermediate data if specified
        if store_intermediate_result and e % 1000 == 0:
            torch.save({
                        'episode': e,
                        'frame': t,
                        'network_state_dict': network.state_dict(),
                        'avg_return': avg_return,
                        'returns': returns,
                        'frame_stamps': frame_stamps,
            }, output_file_name + "_checkpoint")

    # Print final logging info
    logging.info("Avg return: " + str(numpy.around(avg_return, 2)) + " | Time per frame: " +
                 str((time.time()-t_start)/t) )
        
    # Write data to file
    torch.save({
        'returns': returns,
        'frame_stamps': frame_stamps,
        'network_state_dict': network.state_dict()
    }, output_file_name + "_data_and_weights")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--game", "-g", type=str)
    parser.add_argument("--output", "-o", type=str)
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--loadfile", "-l", type=str)
    parser.add_argument("--alpha", "-a", type=float, default=ALPHA)
    parser.add_argument("--save", "-s", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.INFO)

    # If there's an output specified, then use the user specified output. Otherwise, create file in the current
    # directory with the game's name.
    if args.output:
        file_name = args.output
    else:
        file_name = os.getcwd() + "/" + args.game

    load_file_path = None
    if args.loadfile:
        load_file_path = args.loadfile

    env = Velenvironment(args.game)

    print('Cuda available?:'+str(torch.cuda.is_available()))
    AC_lambda(env, file_name, args.save, load_file_path, alpha=args.alpha)


if __name__ == '__main__':
    main()


