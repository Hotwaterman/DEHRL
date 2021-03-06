import torch
from torch.utils.data.sampler import BatchSampler, SubsetRandomSampler


class RolloutStorage(object):
    def __init__(self, num_steps, num_processes, obs_shape, input_actions, action_space, state_size, observation_space):
        self.num_steps = num_steps
        self.observation_space = observation_space
        self.observations = torch.zeros(num_steps + 1, num_processes, *obs_shape)
        self.input_actions = torch.zeros(num_steps + 1, num_processes, input_actions.n)
        self.states = torch.zeros(num_steps + 1, num_processes, state_size)
        self.rewards = torch.zeros(num_steps, num_processes, 1)
        self.reward_bounty_raw = torch.zeros(num_steps, num_processes, 1)
        self.value_preds = torch.zeros(num_steps + 1, num_processes, 1)
        self.returns = torch.zeros(num_steps + 1, num_processes, 1)
        self.action_log_probs = torch.zeros(num_steps, num_processes, 1)
        self.action_space = action_space
        if action_space.__class__.__name__ == 'Discrete':
            action_shape = 1
        elif action_space.__class__.__name__ == 'Box':
            action_shape = action_space.shape[0]
        else:
            raise NotImplemented
        self.actions = torch.zeros(num_steps, num_processes, action_shape)
        if action_space.__class__.__name__ == 'Discrete':
            self.actions = self.actions.long()
        self.masks = torch.ones(num_steps + 1, num_processes, 1)

        self.num_steps = num_steps
        self.step = 0

    def cuda(self):
        self.observations = self.observations.cuda()
        self.input_actions = self.input_actions.cuda()
        self.states = self.states.cuda()
        self.rewards = self.rewards.cuda()
        self.reward_bounty_raw = self.reward_bounty_raw.cuda()
        self.value_preds = self.value_preds.cuda()
        self.returns = self.returns.cuda()
        self.action_log_probs = self.action_log_probs.cuda()
        self.actions = self.actions.cuda()
        self.masks = self.masks.cuda()
        return self

    def insert(self, current_obs, state, action, action_log_prob, value_pred, reward, mask):
        self.observations[self.step + 1].copy_(current_obs)
        self.states[self.step + 1].copy_(state)
        self.actions[self.step].copy_(action)
        self.action_log_probs[self.step].copy_(action_log_prob)
        self.value_preds[self.step].copy_(value_pred)
        self.rewards[self.step].copy_(reward)
        self.masks[self.step + 1].copy_(mask)

        self.step = (self.step + 1) % self.num_steps

    def after_update(self):
        self.observations[0].copy_(self.observations[-1])
        self.states[0].copy_(self.states[-1])
        self.masks[0].copy_(self.masks[-1])

    def compute_returns(self, next_value, use_gae, gamma, tau):
        if use_gae:
            self.value_preds[-1] = next_value
            gae = 0
            for step in reversed(range(self.rewards.size(0))):
                delta = self.rewards[step] + gamma * self.value_preds[step + 1] * self.masks[step + 1] - self.value_preds[step]
                gae = delta + gamma * tau * self.masks[step + 1] * gae
                self.returns[step] = gae + self.value_preds[step]
        else:
            self.returns[-1] = next_value
            for step in reversed(range(self.rewards.size(0))):
                self.returns[step] = self.returns[step + 1] * \
                    gamma * self.masks[step + 1] + self.rewards[step]


    def feed_forward_generator(self, advantages, mini_batch_size):
        num_steps, num_processes = self.rewards.size()[0:2]
        batch_size = num_processes * num_steps
        sampler = BatchSampler(SubsetRandomSampler(range(batch_size)), mini_batch_size, drop_last=False)
        for indices in sampler:
            observations_batch         = self.observations [:-1].view(-1,*self.observations .size()[2:])[indices]
            input_actions_batch        = self.input_actions[:-1].view(-1,*self.input_actions.size()[2:])[indices]
            states_batch               = self.states       [:-1].view(-1, self.states.size(-1)         )[indices]
            actions_batch              = self.actions           .view(-1, self.actions.size(-1)        )[indices]
            return_batch               = self.returns      [:-1].view(-1, 1                            )[indices]
            masks_batch                = self.masks        [:-1].view(-1, 1                            )[indices]
            old_action_log_probs_batch = self.action_log_probs  .view(-1, 1                            )[indices]
            adv_targ                   = advantages             .view(-1, 1                            )[indices]

            yield observations_batch, input_actions_batch, states_batch, actions_batch, \
                return_batch, masks_batch, old_action_log_probs_batch, adv_targ

    def transition_model_feed_forward_generator(self, mini_batch_size, recent_steps=None, recent_at=None):

        observations_batch           = self.observations
        reward_bounty_raw_batch      = self.reward_bounty_raw
        next_observations_batch      = self.observations
        actions_batch                = self.actions
        next_masks_batch             = self.masks

        if recent_steps is not None:
            '''get recent and flatten'''
            observations_batch           = observations_batch          [recent_at-recent_steps:recent_at+1]
            reward_bounty_raw_batch      = reward_bounty_raw_batch     [recent_at-recent_steps:recent_at  ]
            next_observations_batch      = next_observations_batch     [recent_at-recent_steps:recent_at+1]
            actions_batch                = actions_batch               [recent_at-recent_steps:recent_at  ]
            next_masks_batch             = next_masks_batch            [recent_at-recent_steps:recent_at+1]

        observations_batch           = observations_batch               [ :-1].view(-1,*self.observations.size()[2:])
        reward_bounty_raw_batch      = reward_bounty_raw_batch                .view(-1, 1                           )
        next_observations_batch      = next_observations_batch          [1:  ].view(-1,*self.observations.size()[2:])
        actions_batch                = actions_batch                          .view(-1, self.actions.size(-1)       )
        next_masks_batch             = next_masks_batch                 [1:  ].view(-1, 1                           )

        '''generate indexs'''
        try:
            next_masks_batch_index = next_masks_batch.squeeze(1).nonzero().squeeze(1)
        except Exception as e:
            yield None 

        if len(observations_batch.size()) == 4:
            unsqueezed_next_masks_batch_index_for_obs = next_masks_batch_index.unsqueeze(1).unsqueeze(2).unsqueeze(3)
        elif len(observations_batch.size()) == 2:
            unsqueezed_next_masks_batch_index_for_obs = next_masks_batch_index.unsqueeze(1)
        unsqueezed_next_masks_batch_index_for_vec =  next_masks_batch_index.unsqueeze(1)

        next_masks_batch_index_observations_batch      = unsqueezed_next_masks_batch_index_for_obs.expand(next_masks_batch_index.size()[0],*observations_batch     .size()[1:])
        next_masks_batch_index_reward_bounty_raw_batch = unsqueezed_next_masks_batch_index_for_vec                          .expand(next_masks_batch_index.size()[0],*reward_bounty_raw_batch.size()[1:])
        next_masks_batch_index_next_observations_batch = unsqueezed_next_masks_batch_index_for_obs.expand(next_masks_batch_index.size()[0],*next_observations_batch.size()[1:])
        next_masks_batch_index_actions_batch           = unsqueezed_next_masks_batch_index_for_vec                          .expand(next_masks_batch_index.size()[0],*actions_batch          .size()[1:])

        '''index'''
        observations_batch      = observations_batch     .gather(0,next_masks_batch_index_observations_batch)
        reward_bounty_raw_batch = reward_bounty_raw_batch.gather(0,next_masks_batch_index_reward_bounty_raw_batch)
        next_observations_batch = next_observations_batch.gather(0,next_masks_batch_index_next_observations_batch)
        actions_batch           = actions_batch          .gather(0,next_masks_batch_index_actions_batch)

        '''convert actions_batch to action_onehot_batch'''
        action_onehot_batch = torch.zeros(observations_batch.size()[0],self.action_space.n).cuda().fill_(0.0).scatter_(1,actions_batch.long(),1.0)

        batch_size = observations_batch.size()[0]

        if batch_size < 2*mini_batch_size:
            '''if only one batch can be sampled'''
            mini_batch_size = batch_size

        sampler = BatchSampler(SubsetRandomSampler(range(batch_size)), mini_batch_size, drop_last=True)
        for indices in sampler:
            yield observations_batch[indices], next_observations_batch[indices][:,-self.observation_space.shape[0]:], action_onehot_batch[indices], reward_bounty_raw_batch[indices]

    def recurrent_generator(self, advantages, num_mini_batch):
        raise Exception('Not supported')
        num_processes = self.rewards.size(1)
        num_envs_per_batch = num_processes // num_mini_batch
        perm = torch.randperm(num_processes)
        for start_ind in range(0, num_processes, num_envs_per_batch):
            observations_batch = []
            input_actions_batch = []
            states_batch = []
            actions_batch = []
            return_batch = []
            masks_batch = []
            old_action_log_probs_batch = []
            adv_targ = []

            for offset in range(num_envs_per_batch):
                ind = perm[start_ind + offset]
                observations_batch.append(self.observations[:-1, ind])
                input_actions_batch.append(self.input_actions[:-1, ind])
                states_batch.append(self.states[0:1, ind])
                actions_batch.append(self.actions[:, ind])
                return_batch.append(self.returns[:-1, ind])
                masks_batch.append(self.masks[:-1, ind])
                old_action_log_probs_batch.append(self.action_log_probs[:, ind])
                adv_targ.append(advantages[:, ind])

            observations_batch = torch.cat(observations_batch, 0)
            input_actions_batch = torch.cat(input_actions_batch, 0)
            states_batch = torch.cat(states_batch, 0)
            actions_batch = torch.cat(actions_batch, 0)
            return_batch = torch.cat(return_batch, 0)
            masks_batch = torch.cat(masks_batch, 0)
            old_action_log_probs_batch = torch.cat(old_action_log_probs_batch, 0)
            adv_targ = torch.cat(adv_targ, 0)

            yield observations_batch, input_actions_batch, states_batch, actions_batch, \
                return_batch, masks_batch, old_action_log_probs_batch, adv_targ
