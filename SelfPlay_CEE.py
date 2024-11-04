"""
Based on PureJaxRL Implementation of PPO
"""

import jax
import jax.numpy as jnp
import flax.linen as nn
import numpy as np
import optax
from flax.linen.initializers import constant, orthogonal
from typing import Sequence, NamedTuple, Any, Dict
from flax.training.train_state import TrainState
import distrax
from baselines import LogWrapper
import jaxmarl
import wandb
import functools
import matplotlib.pyplot as plt
import hydra
from omegaconf import OmegaConf
from registration import make
import os

class ScannedLSTM(nn.Module):
    @functools.partial(
        nn.scan,
        variable_broadcast="params",
        in_axes=0,
        out_axes=0,
        split_rngs={"params": False},
    )
    @nn.compact
    def __call__(self, cell_state, x):
        (ins, resets) = x
        lstm_carry, lstm_hidden = self.initialize_carry(ins.shape[0], ins.shape[1])
        lstm_state = (
            jnp.where(resets[:, np.newaxis], lstm_carry, cell_state[0]), 
            jnp.where(resets[:, np.newaxis], lstm_hidden, cell_state[1])
        ) 
        (lstm_carry, lstm_hidden), y = nn.OptimizedLSTMCell(features=ins.shape[1])(lstm_state, ins)
        return (lstm_carry, lstm_hidden), y

    @staticmethod
    def initialize_carry(batch_size, hidden_size):
        cell = nn.OptimizedLSTMCell(features=hidden_size)
        return cell.initialize_carry(
            jax.random.PRNGKey(0), (batch_size, hidden_size)
        )

class ActorCriticLSTM(nn.Module):
    action_dim: Sequence[int]
    config: Dict

    @nn.compact
    def __call__(self, carry, hidden, x):
        obs, dones, avail_actions = x
        embedding = nn.Dense(
            self.config["FC_DIM_SIZE"], kernel_init=orthogonal(np.sqrt(2)), bias_init=constant(0.0)
        )(obs)
        embedding = nn.gelu(embedding, approximate=False)

        lstm_in = (embedding, dones)
        (carry, hidden), embedding = ScannedLSTM()((carry, hidden), lstm_in)

        actor_mean = nn.Dense(self.config["LSTM_HIDDEN_DIM"], kernel_init=orthogonal(2), bias_init=constant(0.0))(
            embedding
        )

        actor_mean = nn.gelu(actor_mean, approximate=False)

        actor_mean = nn.Dense(
            self.action_dim, kernel_init=orthogonal(0.01), bias_init=constant(0.0)
        )(actor_mean)
        unavail_actions = 1 - avail_actions
        action_logits = actor_mean - (unavail_actions * 1e10)
        pi = distrax.Categorical(logits=action_logits)

        critic = nn.Dense(self.config["FC_DIM_SIZE"], kernel_init=orthogonal(2), bias_init=constant(0.0))(
            embedding
        )
        critic = nn.gelu(critic, approximate=False)
        critic = nn.Dense(1, kernel_init=orthogonal(1.0), bias_init=constant(0.0))(
            critic
        )

        return carry, hidden, pi, jnp.squeeze(critic, axis=-1)
        
class Transition(NamedTuple):
    global_done: jnp.ndarray
    done: jnp.ndarray
    action: jnp.ndarray
    value: jnp.ndarray
    reward: jnp.ndarray
    log_prob: jnp.ndarray
    obs: jnp.ndarray
    info: jnp.ndarray
    avail_actions: jnp.ndarray

# Batchify to parallelize with Jax
def batchify(x: dict, agent_list, num_actors):
    x = jnp.stack([x[a] for a in agent_list])
    return x.reshape((num_actors, -1))

def unbatchify(x: jnp.ndarray, agent_list, num_envs, num_actors):
    x = x.reshape((num_actors, num_envs, -1))
    return {a: x[i] for i, a in enumerate(agent_list)}

def make_train(config):
    env = make(config["ENV_NAME"], **config["ENV_KWARGS"])
    config["NUM_ACTORS"] = env.num_agents * config["NUM_ENVS"]
    config["NUM_UPDATES"] = (
            config["TOTAL_TIMESTEPS"] // config["NUM_STEPS"] // config["NUM_ENVS"]
    ) 
    config["MINIBATCH_SIZE"] = (
            config["NUM_ACTORS"] * config["NUM_STEPS"] // config["NUM_MINIBATCHES"]
    )

    env = LogWrapper(env)

    def linear_schedule(count):
        frac = 1.0 - (count // (config["NUM_MINIBATCHES"] * config["UPDATE_EPOCHS"])) / config["NUM_UPDATES"]
        return config["LR"] * frac

    def train(rng):
        network = ActorCriticLSTM(env.action_space(env.agents[0]).n, config=config)
        rng, _rng = jax.random.split(rng)
        init_x = (
            jnp.zeros(
                (1, config["NUM_ENVS"], env.observation_space(env.agents[0]).n)
            ),
            jnp.zeros((1, config["NUM_ENVS"])),
            jnp.zeros((1, config["NUM_ENVS"], env.action_space(env.agents[0]).n))
        )
        (init_cstate, init_hstate) = ScannedLSTM.initialize_carry(config["NUM_ENVS"], config["LSTM_HIDDEN_DIM"])
        network_params = network.init(_rng, init_cstate, init_hstate, init_x)
        if config["ANNEAL_LR"]:
            tx = optax.chain(
                optax.clip_by_global_norm(config["MAX_GRAD_NORM"]),
                optax.adam(learning_rate=linear_schedule, eps=1e-5),
            )
        else:
            tx = optax.chain(optax.clip_by_global_norm(config["MAX_GRAD_NORM"]), optax.adam(config["LR"], eps=1e-5))
        train_state = TrainState.create(
            apply_fn=network.apply,
            params=network_params,
            tx=tx,
        )

        rng, _rng = jax.random.split(rng)
        reset_rng = jax.random.split(_rng, config["NUM_ENVS"])
        obsv, env_state = jax.vmap(env.reset, in_axes=(0,))(reset_rng)
        (init_cstate, init_hstate) = ScannedLSTM.initialize_carry(config["NUM_ACTORS"], config["LSTM_HIDDEN_DIM"])

        def _update_step(update_runner_state, unused):
            runner_state, update_steps = update_runner_state
            def _env_step(runner_state, unused):
                train_state, env_state, last_obs, last_done, cstate, hstate, rng = runner_state
                rng, _rng = jax.random.split(rng)
                avail_actions = jax.vmap(env.get_pos_moves)(env_state.env_state)
                avail_actions = jax.lax.stop_gradient(
                    batchify(avail_actions, env.agents, config["NUM_ACTORS"])
                )
                obs_batch = batchify(last_obs, env.agents, config["NUM_ACTORS"])
                ac_in = (obs_batch[np.newaxis, :], last_done[np.newaxis, :], avail_actions[np.newaxis, :])
                cstate, hstate, pi, value = network.apply(train_state.params, cstate, hstate, ac_in)
                action = pi.sample(seed=_rng)
                log_prob = pi.log_prob(action)
                self_env_act = unbatchify(action, env.agents, config["NUM_ENVS"], env.num_agents)
                env_act = {}
                env_act[env.agents[0]] = self_env_act[env.agents[0]]
                env_act[env.agents[1]] = self_env_act[env.agents[1]]
                rng, _rng = jax.random.split(rng)
                rng_step = jax.random.split(_rng, config["NUM_ENVS"])
                obsv, env_state, reward, done, info = jax.vmap(env.step, in_axes=(0, 0, 0))(
                    rng_step, env_state, env_act
                )
                done_batch = batchify(done, env.agents, config["NUM_ACTORS"]).squeeze()
                transition = Transition(
                    jnp.tile(done["__all__"], env.num_agents),
                    last_done,
                    action.squeeze(),
                    value.squeeze(),
                    batchify(reward, env.agents, config["NUM_ACTORS"]).squeeze(), 
                    log_prob.squeeze(),
                    obs_batch,
                    info,
                    avail_actions
                )
                runner_state = (train_state, env_state, obsv, done_batch, cstate, hstate, rng)
                return runner_state, transition
            initial_hstate = runner_state[-2]
            initial_cstate = runner_state[-3]
            runner_state, traj_batch = jax.lax.scan(
                _env_step, runner_state, None, config["NUM_STEPS"]
            )

            train_state, env_state, last_obs, last_done, cstate, hstate, rng = runner_state
            last_obs_batch = batchify(last_obs, env.agents, config["NUM_ACTORS"])
            avail_actions = jnp.ones(
                (config["NUM_ACTORS"], env.action_space(env.agents[0]).n)
            )
            ac_in = (last_obs_batch[np.newaxis, :], last_done[np.newaxis, :], avail_actions)
            _, _, _, last_val  = network.apply(train_state.params, cstate, hstate, ac_in)
            last_val = last_val.squeeze()

            def _calculate_gae(traj_batch, last_val):
                def _get_advantages(gae_and_next_value, transition):
                    gae, next_value = gae_and_next_value
                    done, value, reward = (
                        transition.global_done,
                        transition.value,
                        transition.reward,
                    )
                    delta = reward + config["GAMMA"] * next_value * (1 - done) - value
                    gae = (
                            delta
                            + config["GAMMA"] * config["GAE_LAMBDA"] * (1 - done) * gae
                    )
                    return (gae, value), gae

                _, advantages = jax.lax.scan(
                    _get_advantages,
                    (jnp.zeros_like(last_val), last_val),
                    traj_batch,
                    reverse=True,
                    unroll=16,
                )
                return advantages, advantages + traj_batch.value

            advantages, targets = _calculate_gae(traj_batch, last_val)

            def _update_epoch(update_state, unused):
                def _update_minbatch(train_state, batch_info):
                    init_cstate, init_hstate, traj_batch, advantages, targets = batch_info

                    def _loss_fn(params, init_cstate, init_hstate, traj_batch, gae, targets):
                        _, _, pi, value = network.apply(params, init_cstate.squeeze(), init_hstate.squeeze(),
                                                  (traj_batch.obs, traj_batch.done, traj_batch.avail_actions))
                        log_prob = pi.log_prob(traj_batch.action)

                        value_pred_clipped = traj_batch.value + (
                                value - traj_batch.value
                        ).clip(-config["CLIP_EPS"], config["CLIP_EPS"])
                        value_losses = jnp.square(value - targets)
                        value_losses_clipped = jnp.square(value_pred_clipped - targets)
                        value_loss = 0.5 * (
                                jnp.maximum(value_losses, value_losses_clipped)
                        ).mean()

                        logratio = log_prob - traj_batch.log_prob
                        ratio = jnp.exp(logratio)
                        gae = (gae - gae.mean()) / (gae.std() + 1e-8)
                        loss_actor1 = ratio * gae
                        loss_actor2 = (
                                jnp.clip(
                                    ratio,
                                    1.0 - config["CLIP_EPS"],
                                    1.0 + config["CLIP_EPS"],
                                )
                                * gae
                        )
                        loss_actor = -jnp.minimum(loss_actor1, loss_actor2)
                        loss_actor = loss_actor.mean()
                        entropy = pi.entropy().mean()

                        approx_kl = ((ratio - 1) - logratio).mean()
                        clip_frac = jnp.mean(jnp.abs(ratio - 1) > config["CLIP_EPS"])

                        total_loss = (
                                loss_actor
                                + config["VF_COEF"] * value_loss
                                - config["ENT_COEF"] * entropy
                        )
                        return total_loss, (value_loss, loss_actor, entropy, ratio, approx_kl, clip_frac)

                    grad_fn = jax.value_and_grad(_loss_fn, has_aux=True)
                    total_loss, grads = grad_fn(
                        train_state.params, init_cstate, init_hstate, traj_batch, advantages, targets
                    )
                    def apply_grads_if_needed(approx_kl, target_kl, train_state, grads):
                        apply_grads = approx_kl < target_kl
                        return jax.lax.cond(apply_grads,
                            lambda _: train_state.apply_gradients(grads=grads),
                            lambda _: train_state,
                            operand=None)
                    curr_loss, (value_loss, loss_actor, entropy, ratio, approx_kl, clip_frac) = total_loss
                    train_state = apply_grads_if_needed(approx_kl, config["TARGET_KL"], train_state, grads)
                    approx_kl = jax.lax.select(approx_kl < config["TARGET_KL"], approx_kl, 0*approx_kl)
                    total_loss = curr_loss, (value_loss, loss_actor, entropy, ratio, approx_kl, clip_frac)
                    return train_state, total_loss

                train_state, init_cstate, init_hstate, traj_batch, advantages, targets, rng = update_state
                rng, _rng = jax.random.split(rng)

                init_hstate = jnp.reshape(
                    init_hstate, (1, config["NUM_ACTORS"], -1)
                )

                init_cstate = jnp.reshape(
                    init_cstate, (1, config["NUM_ACTORS"], -1)
                )

                batch = (init_cstate, init_hstate, traj_batch, advantages.squeeze(), targets.squeeze())
                permutation = jax.random.permutation(_rng, config["NUM_ACTORS"])

                shuffled_batch = jax.tree_util.tree_map(
                    lambda x: jnp.take(x, permutation, axis=1), batch
                )

                minibatches = jax.tree_util.tree_map(
                    lambda x: jnp.swapaxes(
                        jnp.reshape(
                            x,
                            [x.shape[0], config["NUM_MINIBATCHES"], -1]
                            + list(x.shape[2:]),
                        ),
                        1,
                        0,
                    ),
                    shuffled_batch,
                ) 

                train_state, total_loss = jax.lax.scan(
                    _update_minbatch, train_state, minibatches
                )
                update_state = (train_state, init_cstate.squeeze(), init_hstate.squeeze(), traj_batch, advantages, targets, rng)
                return update_state, total_loss

            update_state = (train_state, initial_cstate, initial_hstate, traj_batch, advantages, targets, rng)
            update_state, loss_info = jax.lax.scan(
                _update_epoch, update_state, None, config["UPDATE_EPOCHS"]
            )
            train_state = update_state[0]
            metric = traj_batch.info
            ratio_0 = loss_info[1][3].at[0,0].get().mean()
            loss_info = jax.tree_map(lambda x: x.mean(), loss_info)
            metric["loss"] = {
                "total_loss": loss_info[0],
                "value_loss": loss_info[1][0],
                "actor_loss": loss_info[1][1],
                "entropy": loss_info[1][2],
                "ratio": loss_info[1][3],
                "ratio_0": ratio_0,
                "approx_kl": loss_info[1][4],
                "clip_frac": loss_info[1][5],
            }
            rng = update_state[-1]
            def lever_callback(metric):
                wandb.log(
                    {
                        "returns": metric["returned_episode_returns"][-1, :].mean(),
                        "env_step": metric["update_steps"]
                        * config["NUM_ENVS"]
                        * config["NUM_STEPS"],
                        "rewards": metric["agent_0"],
                        "raw rewards": metric["agent_0"]["reward"][-1, :].mean(),
                        "rewards_delta": metric["agent_0"]["reward_delta"][-1, :].mean(),
                        "non_coord": metric["agent_0"]["non_coord"][-1, :].mean(),
                        "true_action1": metric["agent_0"]["true_action1"][-1, :].mean(),
                        "true_action2": metric["agent_0"]["true_action2"][-1, :].mean(),
                        "max_reward": metric["agent_0"]["max_reward"][-1, :].mean(),
                        "regret": metric["agent_0"]["regret"][-1, :].mean()
                    }
            )
            def callback(metric):
                num_timesteps = len(metric["agent_0"]["high_reward_box_coord"])
                last_10pct_start = int(0.9 * num_timesteps)
                last_25pct_start = int(0.75 * num_timesteps)
                wandb.log(
                    {
                        "returns": metric["returned_episode_returns"][-1, :].mean(),
                        "env_step": metric["update_steps"]
                        * config["NUM_ENVS"]
                        * config["NUM_STEPS"],
                        "raw rewards": metric["agent_0"]["reward"][:, :].mean(),
                        "rewards_delta": metric["agent_0"]["reward_delta"][:, :].mean(),
                        "non_coord_action": metric["agent_0"]["non_coord"][:, :].mean(),
                        "true_action1": metric["agent_0"]["true_action1"][:, :].mean(),
                        "true_action2": metric["agent_0"]["true_action2"][:, :].mean(),
                        "max_reward": metric["agent_0"]["max_reward"][:, :].mean(),
                        "regret": metric["agent_0"]["regret"][:, :].mean(),
                        "agent_pos_coord": metric["agent_0"]["agent_pos_coord"][:, :].mean(),
                        "final_step_agent2_pos_x": metric["agent_0"]["agent2_pos_x"][-1, :].mean(),
                        "final_step_agent2_pos_y": metric["agent_0"]["agent2_pos_y"][-1, :].mean(),
                        "final_step_agent1_pos_x": metric["agent_0"]["agent1_pos_x"][-1, :].mean(),
                        "final_step_agent1_pos_y": metric["agent_0"]["agent1_pos_y"][-1, :].mean(),
                        "high_reward_box_coord": metric["agent_0"]["high_reward_box_coord"][:, :].mean(),
                        "mid_reward_box_coord": metric["agent_0"]["mid_reward_box_coord"][:, :].mean(),
                        "low_reward_box_coord": metric["agent_0"]["low_reward_box_coord"][:, :].mean(),
                        "key_metrics/final_step_high_reward_box_coord": metric["agent_0"]["high_reward_box_coord"][-1, :].mean(),
                        "key_metrics/final_step_mid_reward_box_coord": metric["agent_0"]["mid_reward_box_coord"][-1, :].mean(),
                        "key_metrics/final_step_low_reward_box_coord": metric["agent_0"]["low_reward_box_coord"][-1, :].mean(),
                        "key_metrics/final_step_agent_pos_coord": metric["agent_0"]["agent_pos_coord"][-1, :].mean(),
                        "key_metrics/final_step_rewards_delta": metric["agent_0"]["reward_delta"][-1, :].mean(),
                        "key_metrics/final_step_avg_reward": metric["agent_0"]["reward"][-1, :].mean(),
                        "key_metrics/final_step_agent_1_key_coord": metric["agent_0"]["agent_1_key"][-1, :].mean(),
                        "key_metrics/final_step_agent_2_key_coord": metric["agent_0"]["agent_2_key"][-1, :].mean(),
                        "key_metrics/final_step_agent_1_stationary_reward_coord": metric["agent_0"]["agent_1_stationary_reward"][-1, :].mean(),
                        "key_metrics/final_step_agent_2_stationary_reward_coord": metric["agent_0"]["agent_2_stationary_reward"][-1, :].mean(),
                        "key_metrics/final_step_avg_reward": metric["agent_0"]["reward"][-1, :].mean(),
                        "key_metrics/final_step_regret": metric["agent_0"]["regret"][-1, :].mean(),
                        "key_metrics/final_step_reward_box_coord": (metric["agent_0"]["low_reward_box_coord"][-1, :] + metric["agent_0"]["high_reward_box_coord"][-1, :]+ metric["agent_0"]["mid_reward_box_coord"][-1, :]).mean(),
                        "key_metrics/final_step_total_reward": metric["agent_0"]["total_reward"][-1, :].mean(),
                        "key_metrics/last_non_stationary_move": metric["agent_0"]["last_non_stationary_move"][-1, :].mean(),
                        "key_metrics/agent_1_reward_box_stepped_on": metric["agent_0"]["agent_1_reward_box_stepped_on"][-1, :].mean(),
                        "key_metrics/agent_2_reward_box_stepped_on": metric["agent_0"]["agent_2_reward_box_stepped_on"][-1, :].mean(),
                        "key_metrics/both_agent_coord_before_stationary": metric["agent_0"]["both_agent_coord_before_stationary"][-1, :].mean(),
                        "last_10pct/last_10pct_high_reward_box_coord": metric["agent_0"]["high_reward_box_coord"][last_10pct_start:, :].mean(),
                        "last_10pct/last_10pct_mid_reward_box_coord": metric["agent_0"]["mid_reward_box_coord"][last_10pct_start:, :].mean(),
                        "last_10pct/last_10pct_low_reward_box_coord": metric["agent_0"]["low_reward_box_coord"][last_10pct_start:, :].mean(),
                        "last_10pct/last_10pct_agent_pos_coord": metric["agent_0"]["agent_pos_coord"][last_10pct_start:, :].mean(),
                        "last_10pct/last_10pct_rewards_delta": metric["agent_0"]["reward_delta"][last_10pct_start:, :].mean(),
                        "last_10pct/last_10pct_regret": metric["agent_0"]["regret"][last_10pct_start:, :].mean(),
                        "last_10pct/last_10pct_reward_box_coord": (metric["agent_0"]["low_reward_box_coord"][last_10pct_start:, :] + metric["agent_0"]["high_reward_box_coord"][last_10pct_start:, :] + metric["agent_0"]["mid_reward_box_coord"][last_10pct_start:, :]).mean(),
                        "last_10pct/last_10pct_total_reward": metric["agent_0"]["total_reward"][last_10pct_start:, :].mean(),
                        "last_25pct/last_25pct_high_reward_box_coord": metric["agent_0"]["high_reward_box_coord"][last_25pct_start:, :].mean(),
                        "last_25pct/last_25pct_mid_reward_box_coord": metric["agent_0"]["mid_reward_box_coord"][last_25pct_start:, :].mean(),
                        "last_25pct/last_25pct_low_reward_box_coord": metric["agent_0"]["low_reward_box_coord"][last_25pct_start:, :].mean(),
                        "last_25pct/last_25pct_agent_pos_coord": metric["agent_0"]["agent_pos_coord"][last_25pct_start:, :].mean(),
                        "last_25pct/last_25pct_rewards_delta": metric["agent_0"]["reward_delta"][last_25pct_start:, :].mean(),
                        "last_25pct/last_25pct_regret": metric["agent_0"]["regret"][last_25pct_start:, :].mean(),
                        "last_25pct/last_25pct_reward_box_coord": (metric["agent_0"]["low_reward_box_coord"][last_25pct_start:, :] + metric["agent_0"]["high_reward_box_coord"][last_25pct_start:, :] + metric["agent_0"]["mid_reward_box_coord"][last_25pct_start:, :]).mean(),
                        "last_25pct/last_25pct_total_reward": metric["agent_0"]["total_reward"][last_25pct_start:, :].mean(),
                        "high_reward_box_pos_from_start": metric["agent_0"]["high_reward_box_pos"][-1, :].mean(),
                        "mid_reward_box_pos_from_start": metric["agent_0"]["mid_reward_box_pos"][-1, :].mean(),
                        "low_reward_box_pos_from_start": metric["agent_0"]["low_reward_box_pos"][-1, :].mean(),
                        "high_reward": metric["agent_0"]["high_reward"][-1, :].mean(),
                        "mid_reward": metric["agent_0"]["mid_reward"][-1, :].mean(),
                        "low_reward": metric["agent_0"]["low_reward"][-1, :].mean(),
                        "loss/total_loss": metric["loss_info"][0].mean(),
                        "loss/value_loss": metric["loss_info"][1][0].mean(),
                        "loss/loss_actor": metric["loss_info"][1][1].mean(),
                        "loss/entropy": metric["loss_info"][1][2].mean(),
                        "loss/total_loss": metric["loss"]["total_loss"],
                        "loss/value_loss": metric["loss"]["value_loss"],
                        "loss/entropy": metric["loss"]["entropy"],
                        "loss/ratio": metric["loss"]["ratio"],
                        "loss/ratio_0": metric["loss"]["ratio_0"],
                        "loss/approx_kl": metric["loss"]["approx_kl"],
                        "loss/clip_frac": metric["loss"]["clip_frac"],
                        "agent_1_num_up_actions": jnp.sum(metric["agent_0"]["true_action1"] == 0),
                        "agent_1_num_down_actions": jnp.sum(metric["agent_0"]["true_action1"] == 1),
                        "agent_1_num_right_actions": jnp.sum(metric["agent_0"]["true_action2"] == 2),
                        "agent_1_num_left_actions": jnp.sum(metric["agent_0"]["true_action1"] == 3),
                        "agent_1_num_stay_actions": jnp.sum(metric["agent_0"]["true_action1"] == 4),
                        "agent_2_num_up_actions": jnp.sum(metric["agent_0"]["true_action2"] == 0),
                        "agent_2_num_down_actions": jnp.sum(metric["agent_0"]["true_action2"] == 1),
                        "agent_2_num_right_actions": jnp.sum(metric["agent_0"]["true_action2"] == 2),
                        "agent_2_num_left_actions": jnp.sum(metric["agent_0"]["true_action2"] == 3),
                        "agent_2_num_stay_actions": jnp.sum(metric["agent_0"]["true_action2"] == 4),
                    }
                )

            metric["update_steps"] = update_steps
            metric["loss_info"] = loss_info
            jax.experimental.io_callback(callback, None, metric)
            update_steps = update_steps + 1
            runner_state = (train_state, env_state, last_obs, last_done, cstate, hstate, rng)
            return (runner_state, update_steps), (metric["agent_0"]["regret"][-1, :].mean(), metric["agent_0"]["non_coord"][-1, :].mean())

        rng, _rng = jax.random.split(rng)
        runner_state = (train_state, env_state, obsv, jnp.zeros((config["NUM_ACTORS"]), dtype=bool), init_cstate, init_hstate, _rng)
        runner_state, (regret, non_coord) = jax.lax.scan(
            _update_step, (runner_state, 0), None, config["NUM_UPDATES"]
        )
        return {"runner_state": runner_state, "regret": regret, "non_coord": non_coord}
    return train



@hydra.main(version_base=None, config_path="config", config_name="CEE")
def main(config):
    config = OmegaConf.to_container(config)
    for _ in range(8):
        run_name = "SelfPlay_CEE_" + str(config["SEED"] + counter)

        wandb.init(
            entity=config["ENTITY"],
            project=config["PROJECT"],
            tags=["IPPO", "LSTM", config["ENV_NAME"]],
            config=config,
            mode=config["WANDB_MODE"],
            name=run_name,
        )

        rng = jax.random.PRNGKey(config["SEED"])
        train_jit = jax.jit(make_train(config), device=jax.devices()[0])
        out = train_jit(rng)
        final_train_state = out["runner_state"][0][0]
        agent_params = final_train_state.params

        agent_weight = os.path.join(
            f"SelfPlay_CEE_",
            f"agent_{config['SEED']}_param_weights.npz",
        )
        with open(agent_weight, "wb") as f:
            jnp.save(f, agent_params)
        wandb.finish()
        counter += 1
if __name__ == "__main__":
    main()