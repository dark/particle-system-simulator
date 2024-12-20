#!/usr/bin/python3
#
#  Simulate and display movement of particles in a system
#  Copyright (C) 2024  Marco Leogrande
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

import attrs
import copy
import math
import numpy as np
import scipy
from util import Utils
from randomizer import Randomizer
from typing import List, Optional


@attrs.define
class State:
    # Particles state
    p: np.typing.NDArray  # (n, d) vector of position state
    v: np.typing.NDArray  # (n, d) vector of velocity state
    a: np.typing.NDArray  # (n, d) vector of acceleration state
    # Predators state
    pred_p: np.typing.NDArray  # (n, d) vector of position state
    pred_v: np.typing.NDArray  # (n, d) vector of velocity state
    pred_a: np.typing.NDArray  # (n, d) vector of acceleration state


@attrs.frozen(kw_only=True)
class Config:
    # Species-wide config
    v_max: float  # max linear velocity (species-wide)
    v_decay: float  # exponential decay parameter for the velocity vector (species-wide)
    a_max: float  # max linear acceleration (species-wide)
    d_max: float  # max sight distance (species-wide)
    u_max: float  # urgency value (absolute) that corresponds to a_max (species-wide)
    u1_p: float  # linear parameter for the 1st urgency component (species-wide)
    u2_p: float  # linear parameter for the 2nd urgency component (species-wide)
    u2_dopt: float  # optimal distance for the 2nd urgency component (species-wide)
    u3_p: float  # linear parameter for the 3rd urgency component (species-wide)
    u3_dmax: float  # maximum distance that triggers a particle escape for the 3rd urgency component (species-wide)
    # Per-individual config
    uw: np.typing.NDArray  # (n, 3) weights for each urgency component (per-individual)


@attrs.frozen(kw_only=True)
class EngineRunResult:
    # Snapshots of the various states the engine ran through.
    states: List[State]
    # Optionally, the urgency vectors for each step the engine ran
    # through. Each step is represented in an array with shape
    # (urgencies_count, particles_count, dimensions_count).
    urgencies: Optional[List[np.typing.NDArray]]


class Engine:
    _state: State
    _cfg: Config
    _rand: Randomizer

    def __init__(self, state: State, cfg: Config):
        self._state = state
        self._cfg = cfg
        self._rand = Randomizer()

        ## sanity checks
        if (
            self._state.p.shape != self._state.v.shape
            or self._state.p.shape != self._state.a.shape
            or self._cfg.uw.shape != (self._state.p.shape[0], 3)
        ):
            raise ValueError(
                "Inconsistent shapes: p={0} v={1} a={2} uw={3}".format(
                    self._state.p.shape,
                    self._state.v.shape,
                    self._state.a.shape,
                    self._cfg.uw.shape,
                )
            )

    def run(
        self,
        *,
        timestep: float,
        iterations: int,
        skip_initial_states: int = 0,
        return_urgency_vectors: bool = False
    ) -> EngineRunResult:
        """Run the simulation, return a snapshot of all states."""
        print("Starting simulation with {} iterations ...".format(iterations))
        if skip_initial_states > 0:
            print(
                "(the first {} iterations will be simulated but not returned into the state vector)".format(
                    skip_initial_states
                )
            )
            states = []
            urgency_vectors = []
        else:
            states = [copy.deepcopy(self._state)]
            # The shape is (urgencies_count, particles_count, dimensions_count).
            urgency_vectors = [
                np.zeros((3, self._state.p.shape[0], self._state.p.shape[1]))
            ]

        for iteration in range(1, iterations + 1):
            if iteration % 10 == 0:
                print(
                    "\rSimulating iteration {}/{}".format(iteration, iterations), end=""
                )

            urgencies = self._step_particles(timestep, return_urgency_vectors)
            self._step_predators(timestep)
            if iteration > (skip_initial_states - 1):
                # The math works so that skip_initial_states=i will
                # skip the initial state and the (i-1) states after
                # that (hence the -1).
                states.append(copy.deepcopy(self._state))
                if return_urgency_vectors:
                    urgency_vectors.append(urgencies)

        print()
        return EngineRunResult(
            states=states, urgencies=urgency_vectors if return_urgency_vectors else None
        )

    def _step_particles(self, timestep: float, return_urgency_vectors: bool):
        """Execute one step of the simulation for all particles."""
        distances = scipy.spatial.distance.squareform(
            scipy.spatial.distance.pdist(self._state.p)
        )
        # Calculate every single urgency
        u1 = self._calculate_urgency1(distances)
        u2 = self._calculate_urgency2(distances)
        u3 = self._calculate_urgency3(distances)
        # Calculate and clip the total urgency
        u_tot = u1 + u2 + u3
        self._state.a = u_tot / self._cfg.u_max * self._cfg.a_max
        Utils.inplace_clip_by_abs(self._state.a, self._cfg.a_max)
        # Add epsilon uncertainty to final acceleration matrix
        self._state.a *= self._rand.gen_epsilon_matrix(self._state.a.shape)
        # Edit velocity and position state accordingly
        self._state.v *= math.pow(self._cfg.v_decay, timestep)
        self._state.v += self._state.a * timestep
        Utils.inplace_clip_by_abs(self._state.v, self._cfg.v_max)
        self._state.p += self._state.v * timestep
        return np.array([u1, u2, u3]) if return_urgency_vectors else None

    def _step_predators(self, timestep: float):
        """Execute one step of the simulation for all predators."""
        # Predators have no urgency, so their acceleration is constant (except for the epsilon).
        self._state.pred_a *= self._rand.gen_epsilon_matrix(self._state.pred_a.shape)
        # Edit velocity and position state accordingly; there is no
        # velocity decay or cap.
        self._state.pred_v += self._state.pred_a * timestep
        self._state.pred_p += self._state.pred_v * timestep

    def _calculate_urgency1(self, distances):
        """
        Attracts each particle to the baricenter of the other particles in range.
        """
        # Select all particles that, for this component, have an
        # effect on one another.
        in_range = np.logical_and(distances > 0, distances <= self._cfg.d_max)
        # To calculate the baricenter that each particle is affected
        # by, generate a weights matrix first. This matrix embeds how
        # much each other particle contributes to the final baricenter
        # experienced by any given particle.
        weights = np.zeros_like(in_range, dtype=float)
        np.divide(
            1.0,
            np.count_nonzero(in_range, axis=1, keepdims=1),
            where=in_range,
            out=weights,
        )
        # Calculate the baricenter as witnessed by each particle. This
        # is a (n, d) matrix.
        baricenters = weights @ self._state.p
        # Calculate the vector first (with epsilson)
        u1_vector = (baricenters - self._state.p) * self._rand.gen_epsilon_matrix(
            self._state.p.shape
        )
        # Multiply by the appropriate weights.
        return u1_vector * self._cfg.u1_p * self._cfg.uw[:, 0].reshape((-1, 1))

    def _calculate_urgency2(self, distances):
        """Avoids each particle from getting too close to other particles.

        The strenght of this urgency is:

        * when distance < u2_dopt: u2_p * (u2_dopt - distance) / u2_dopt
        *               otherwise: 0

        This means that the urgency is equal to `u2_p` if
        `distance=0`, then it linearly decreases as distance
        decreases, reaching `u2_p=0` when `distance=u2_dopt.`
        """
        # Select all particles that, for this component, have an
        # effect on one another.
        in_range = np.logical_and(distances > 0, distances <= self._cfg.u2_dopt)
        # Generate weights for how much each particle affects another.
        weights = np.zeros_like(in_range, dtype=float)

        # The weight modulates how strongly two particles repel one
        # another based on distance, and is equal to the following
        # component of the urgency:
        #
        # (u2_dopt - distance) / u2_dopt
        #
        # Distance vectors are in the right direction, however they
        # are not unit vectors. For example, two particles that are
        # `u2_dopt` distant generate a vector that is `u2_dopt`
        # long. Hence, we need to normalize each vector to a unit
        # vector by dividing it by its length. Because of this, the
        # final weight formula is:
        #
        # (u2_dopt - distance) / (u2_dopt * distance)
        np.divide(
            self._cfg.u2_dopt - distances,
            self._cfg.u2_dopt * distances,
            where=in_range,
            out=weights,
        )
        # The weights need to be applied to the distance vector
        # existing between each pair of particles. Hence, we need to
        # compute all such vector distances.
        #
        # This results in a (n, n, d) matrix.
        #
        # Note also that the difference is computed as (particle -
        # other_particle), because this is a repulsive force: the
        # vector points from the other particle to the given one. The
        # way which Numpy broadcasting works, this requires
        # "extending" the first array, so we can keep `particle`
        # constant while we "iterate" over `other_particle`.
        distance_vectors = self._state.p[:, np.newaxis] - self._state.p

        # Calculate the total effect on each particle (with
        # epsilon). The operation applies a matrix multiplication
        # separately on each last two dimensions. In other words:
        #
        # (n, a, b) * (n, b, c) -> (n, a, c)
        #
        # for each of the n matrices in the leftmost dimension, compute:
        #
        # (a, b) @ (b, c) = (a, c)
        #
        # See also:
        # https://numpy.org/devdocs/reference/routines.linalg.html#linear-algebra-on-several-matrices-at-once
        #
        # This requires some reshaping on the fly to make dimensions work.
        number_of_points = self._state.p.shape[0]
        u2_vector = np.matmul(
            weights.reshape((number_of_points, 1, number_of_points)), distance_vectors
        ).reshape(number_of_points, -1) * self._rand.gen_epsilon_matrix(
            self._state.p.shape
        )
        # Multiply by the appropriate weights.
        return u2_vector * self._cfg.u2_p * self._cfg.uw[:, 1].reshape((-1, 1))

    def _calculate_urgency3(self, __unused_distances):
        """Repels each particle from specially-designated "predator" particles.

        The strenght of this urgency is:

        * when distance < u3_dmax: u3_p * (u3_dmax - distance) / u3_dmax
        *               otherwise: 0

        This means that the urgency is equal to `u3_p` if
        `distance=0`, then it linearly decreases as distance
        decreases, reaching `u3_p=0` when `distance=u3_dmax.`

        """
        # Many of the steps in this function replicate what has been
        # said in _calculate_urgency2, since they both deal with
        # repulsive forces.
        #
        # The notable difference is that the other function deals with
        # (n, n) matrices, where `n=number of particles`, since the
        # notable distances are all pairwise particle distances. Here,
        # instead, we deal with (n, p) matrices, where `n` has the
        # same meaning, and `p=numver of predators`.

        # This is a (n, p) matrix.
        distances_from_predators = scipy.spatial.distance.cdist(
            self._state.p, self._state.pred_p
        )
        # Select all particles that, for this component, are affected
        # by a predator.
        in_range = np.logical_and(
            distances_from_predators > 0, distances_from_predators <= self._cfg.u3_dmax
        )
        # Generate weights for how much each particle is affected.
        weights = np.zeros_like(in_range, dtype=float)
        np.divide(
            self._cfg.u3_dmax - distances_from_predators,
            self._cfg.u3_dmax * distances_from_predators,
            where=in_range,
            out=weights,
        )

        # This is a (n, p, d) matrix. This is computed as (particle -
        # predator) for the same reasons that apply to the similar
        # computation in _calculate_urgency2.
        distance_vectors = self._state.p[:, np.newaxis] - self._state.pred_p

        # The goal is to generate a u3_vector that is (n, d) in shape,
        # performing the matmul magic:
        #
        # (n, 1, p) * (n, p, d) = (n, 1, d)
        number_of_points = self._state.p.shape[0]
        number_of_predators = self._state.pred_p.shape[0]
        u3_vector = np.matmul(
            weights.reshape((number_of_points, 1, number_of_predators)),
            distance_vectors,
        ).reshape(number_of_points, -1) * self._rand.gen_epsilon_matrix(
            self._state.p.shape
        )
        # Multiply by the appropriate weights.
        return u3_vector * self._cfg.u3_p * self._cfg.uw[:, 2].reshape((-1, 1))
