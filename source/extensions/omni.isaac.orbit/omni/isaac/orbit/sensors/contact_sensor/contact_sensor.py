# Copyright (c) 2022, NVIDIA CORPORATION & AFFILIATES, ETH Zurich, and University of Toronto
# All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause


from __future__ import annotations

import torch
from typing import TYPE_CHECKING, Sequence

import omni.isaac.core.utils.prims as prim_utils
import omni.physics.tensors.impl.api as physx
from omni.isaac.core.prims import RigidContactView, RigidPrimView
from pxr import PhysxSchema

import omni.isaac.orbit.utils.string as string_utils
from omni.isaac.orbit.markers import VisualizationMarkers
from omni.isaac.orbit.markers.config import CONTACT_SENSOR_MARKER_CFG

from ..sensor_base import SensorBase
from .contact_sensor_data import ContactSensorData

if TYPE_CHECKING:
    from .contact_sensor_cfg import ContactSensorCfg


class ContactSensor(SensorBase):
    """A contact reporting sensor.

    The contact sensor reports the normal contact forces on a rigid body in the world frame.
    It relies on the `PhysX ContactReporter`_ API to be activated on the rigid bodies.

    To enable the contact reporter on a rigid body, please make sure to enable the
    :attr:`omni.isaac.orbit.sim.spawner.RigidObjectSpawnerCfg.activate_contact_sensors` on your
    asset spawner configuration. This will enable the contact reporter on all the rigid bodies
    in the asset.

    The sensor can be configured to report the contact forces on a set of bodies with a given
    filter pattern. Please check the documentation on `RigidContactView`_ for more details.

    .. _PhysX ContactReporter: https://docs.omniverse.nvidia.com/kit/docs/omni_usd_schema_physics/104.2/class_physx_schema_physx_contact_report_a_p_i.html
    .. _RigidContactView: https://docs.omniverse.nvidia.com/py/isaacsim/source/extensions/omni.isaac.core/docs/index.html#omni.isaac.core.prims.RigidContactView
    """

    cfg: ContactSensorCfg
    """The configuration parameters."""

    def __init__(self, cfg: ContactSensorCfg):
        """Initializes the contact sensor object.

        Args:
            cfg (ContactSensorCfg): The configuration parameters.
        """
        # initialize base class
        super().__init__(cfg)
        # Create empty variables for storing output data
        self._data: ContactSensorData = ContactSensorData()
        # visualization markers
        self.contact_visualizer = None

    def __str__(self) -> str:
        """Returns: A string containing information about the instance."""
        return (
            f"Contact sensor @ '{self.cfg.prim_path}': \n"
            f"\tview type         : {self._view.__class__}\n"
            f"\tupdate period (s) : {self.cfg.update_period}\n"
            f"\tnumber of bodies  : {self.num_bodies}\n"
            f"\tbody names        : {self.body_names}\n"
        )

    """
    Properties
    """

    @property
    def data(self) -> ContactSensorData:
        # update sensors if needed
        self._update_outdated_buffers()
        # return the data
        return self._data

    @property
    def num_bodies(self) -> int:
        """Number of bodies with contact sensors attached."""
        return self._num_bodies

    @property
    def body_names(self) -> list[str]:
        """Ordered names of bodies with contact sensors attached."""
        prim_paths = self._view.prim_paths[: self.num_bodies]
        return [path.split("/")[-1] for path in prim_paths]

    @property
    def body_view(self) -> RigidPrimView:
        """View for the rigid bodies captured (Isaac Sim)."""
        return self._view

    @property
    def contact_view(self) -> RigidContactView:
        """Contact reporter view for the bodies (Isaac Sim)."""
        return self._view._contact_view  # pyright: ignore [reportPrivateUsage]

    @property
    def body_physx_view(self) -> physx.RigidBodyView:
        """View for the rigid bodies captured (PhysX).

        Note:
            Use this view with caution! It requires handling of tensors in a specific way and is exposed for
            advanced users who have a deep understanding of PhysX SDK. Prefer using the Isaac Sim view when possible.
        """
        return self._view._physics_view  # pyright: ignore [reportPrivateUsage]

    @property
    def contact_physx_view(self) -> physx.RigidContactView:
        """Contact reporter view for the bodies (PhysX).

        Note:
            Use this view with caution! It requires handling of tensors in a specific way and is exposed for
            advanced users who have a deep understanding of PhysX SDK. Prefer using the Isaac Sim view when possible.
        """
        return self._view._contact_view._physics_view  # pyright: ignore [reportPrivateUsage]

    """
    Operations
    """

    def set_debug_vis(self, debug_vis: bool):
        super().set_debug_vis(debug_vis)
        if self.contact_visualizer is not None:
            self.contact_visualizer.set_visibility(debug_vis)

    def reset(self, env_ids: Sequence[int] | None = None):
        # reset the timers and counters
        super().reset(env_ids)
        # resolve None
        if env_ids is None:
            env_ids = ...
        # reset accumulative data buffers
        self._data.current_air_time[env_ids] = 0.0
        self._data.last_air_time[env_ids] = 0.0
        self._data.net_forces_w[env_ids] = 0.0
        # reset the data history
        self._data.net_forces_w_history[env_ids] = 0.0
        # Set all reset sensors to not outdated since their value won't be updated till next sim step.
        self._is_outdated[env_ids] = False

    def find_bodies(self, name_keys: str | Sequence[str]) -> tuple[list[int], list[str]]:
        """Find bodies in the articulation based on the name keys.

        Args:
            name_keys (Union[str, Sequence[str]]): A regular expression or a list of regular expressions
                to match the body names.

        Returns:
            Tuple[List[int], List[str]]: A tuple of lists containing the body indices and names.
        """
        return string_utils.resolve_matching_names(name_keys, self.body_names)

    """
    Implementation.
    """

    def _initialize_impl(self):
        super()._initialize_impl()
        # check that only rigid bodies are selected
        matching_prim_paths = prim_utils.find_matching_prim_paths(self.cfg.prim_path)
        num_prim_matches = len(matching_prim_paths) // self._num_envs
        body_names = list()
        for prim_path in matching_prim_paths[:num_prim_matches]:
            prim = prim_utils.get_prim_at_path(prim_path)
            # check if prim has contact reporter API
            if prim.HasAPI(PhysxSchema.PhysxContactReportAPI):
                body_names.append(prim_path.rsplit("/", 1)[-1])
        # check that there is at least one body with contact reporter API
        if not body_names:
            raise RuntimeError(
                f"Sensor at path '{self.cfg.prim_path}' could not find any bodies with contact reporter API."
                "\nHINT: Make sure to enable 'activate_contact_sensors' in the corresponding asset spawn configuration."
            )
        # construct regex expression for the body names
        body_names_regex = r"(" + "|".join(body_names) + r")"
        body_names_regex = f"{self.cfg.prim_path.rsplit('/', 1)[0]}/{body_names_regex}"
        # construct a new regex expression
        # create a rigid prim view for the sensor
        self._view = RigidPrimView(
            prim_paths_expr=body_names_regex,
            reset_xform_properties=False,
            track_contact_forces=True,
            contact_filter_prim_paths_expr=self.cfg.filter_prim_paths_expr,
            prepare_contact_sensors=False,
            disable_stablization=True,
        )
        self._view.initialize()
        # resolve the true count of bodies
        self._num_bodies = self._view.count // self._num_envs
        # check that contact reporter succeeded
        if self._num_bodies != len(body_names):
            raise RuntimeError(
                f"Failed to initialize contact reporter for specified bodies."
                f"\n\tInput prim path    : {self.cfg.prim_path}"
                f"\n\tResolved prim paths: {body_names_regex}"
            )

        # fill the data buffer
        self._data.pos_w = torch.zeros(self._num_envs, self._num_bodies, 3, device=self._device)
        self._data.quat_w = torch.zeros(self._num_envs, self._num_bodies, 4, device=self._device)
        self._data.last_air_time = torch.zeros(self._num_envs, self._num_bodies, device=self._device)
        self._data.current_air_time = torch.zeros(self._num_envs, self._num_bodies, device=self._device)
        self._data.net_forces_w = torch.zeros(self._num_envs, self._num_bodies, 3, device=self._device)
        self._data.net_forces_w_history = torch.zeros(
            self._num_envs, self.cfg.history_length + 1, self._num_bodies, 3, device=self._device
        )
        # force matrix: (num_sensors, num_bodies, num_shapes, num_filter_shapes, 3)
        if len(self.cfg.filter_prim_paths_expr) != 0:
            num_shapes = self.contact_physx_view.sensor_count // self._num_bodies
            num_filters = self.contact_physx_view.filter_count
            self._data.force_matrix_w = torch.zeros(
                self.count, self._num_bodies, num_shapes, num_filters, 3, device=self._device
            )

    def _update_buffers_impl(self, env_ids: Sequence[int]):
        """Fills the buffers of the sensor data."""
        # default to all sensors
        if len(env_ids) == self._num_envs:
            env_ids = ...
        # obtain the poses of the sensors:
        # TODO decide if we really to track poses -- This is the body's CoM. Not contact location.
        pose = self.body_physx_view.get_transforms()
        self._data.pos_w[env_ids] = pose.view(-1, self._num_bodies, 7)[env_ids, :, :3]
        self._data.quat_w[env_ids] = pose.view(-1, self._num_bodies, 7)[env_ids, :, 3:]

        # obtain the contact forces
        # TODO: We are handling the indexing ourself because of the shape; (N, B) vs expected (N * B).
        #   This isn't the most efficient way to do this, but it's the easiest to implement.
        net_forces_w = self.contact_physx_view.get_net_contact_forces(dt=self._sim_physics_dt)
        self._data.net_forces_w[env_ids, :, :] = net_forces_w.view(-1, self._num_bodies, 3)[env_ids]

        # obtain the contact force matrix
        if len(self.cfg.filter_prim_paths_expr) != 0:
            # shape of the filtering matrix: (num_sensors, num_bodies, num_shapes, num_filter_shapes, 3)
            num_shapes = self.contact_physx_view.sensor_count // self._num_bodies
            num_filters = self.contact_physx_view.filter_count
            # acquire and shape the force matrix
            force_matrix_w = self.contact_physx_view.get_contact_force_matrix(dt=self._sim_physics_dt)
            force_matrix_w = force_matrix_w.view(-1, self._num_bodies, num_shapes, num_filters, 3)
            self._data.force_matrix_w[env_ids] = force_matrix_w[env_ids]

        # update contact force history
        previous_net_forces_w = self._data.net_forces_w_history.clone()
        self._data.net_forces_w_history[env_ids, 0, :, :] = self._data.net_forces_w[env_ids, :, :]
        if self.cfg.history_length > 0:
            self._data.net_forces_w_history[env_ids, 1:, :, :] = previous_net_forces_w[env_ids, :-1, :, :]

        # contact state
        # -- time elapsed since last update
        # since this function is called every frame, we can use the difference to get the elapsed time
        elapsed_time = self._timestamp[env_ids] - self._timestamp_last_update[env_ids]
        # -- check contact state of bodies
        is_contact = torch.norm(self._data.net_forces_w[env_ids, 0, :, :], dim=-1) > 1.0
        is_first_contact = (self._data.current_air_time[env_ids] > 0) * is_contact
        # -- update ongoing timer for bodies air
        self._data.current_air_time[env_ids] += elapsed_time.unsqueeze(-1)
        # -- update time for the last time bodies were in contact
        self._data.last_air_time[env_ids] = self._data.current_air_time[env_ids] * is_first_contact
        # -- increment timers for bodies that are not in contact
        self._data.current_air_time[env_ids] *= ~is_contact

    def _debug_vis_impl(self):
        # visualize the contacts
        if self.contact_visualizer is None:
            self.contact_visualizer = VisualizationMarkers("/Visuals/ContactSensor", cfg=CONTACT_SENSOR_MARKER_CFG)
        # marker indices
        # 0: contact, 1: no contact
        net_contact_force_w = torch.norm(self._data.net_forces_w, dim=-1)
        marker_indices = torch.where(net_contact_force_w > 1.0, 0, 1)
        # check if prim is visualized
        self.contact_visualizer.visualize(self._data.pos_w.view(-1, 3), marker_indices=marker_indices.view(-1))
