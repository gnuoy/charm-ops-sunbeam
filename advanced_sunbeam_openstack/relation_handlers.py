# Copyright 2021 Canonical Ltd.
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

"""Base classes for defining a charm using the Operator framework."""

import json
import logging
from typing import Callable, List, Tuple

import ops.charm
import ops.framework

import charms.nginx_ingress_integrator.v0.ingress as ingress
import charms.sunbeam_mysql_k8s.v0.mysql as mysql
import charms.sunbeam_rabbitmq_operator.v0.amqp as sunbeam_amqp
import charms.sunbeam_keystone_operator.v0.identity_service as sunbeam_id_svc
import advanced_sunbeam_openstack.interfaces as sunbeam_interfaces
import interface_ceph_client.ceph_client as ceph_client

logger = logging.getLogger(__name__)

ERASURE_CODED = "erasure-coded"
REPLICATED = "replacated"


class RelationHandler(ops.charm.Object):
    """Base handler class for relations.

    A relation handler is used to manage a charms interaction with a relation
    interface. This includes:

    1) Registering handlers to process events from the interface. The last
       step of these handlers is to make a callback to a specified method
       within the charm `callback_f`
    2) Expose a `ready` property so the charm can check a relations readyness
    3) A `context` method which returns a dict which pulls together data
       recieved and sent on an interface.
    """

    def __init__(
        self,
        charm: ops.charm.CharmBase,
        relation_name: str,
        callback_f: Callable,
    ) -> None:
        """Run constructor."""
        super().__init__(charm, None)
        self.charm = charm
        self.relation_name = relation_name
        self.callback_f = callback_f
        self.interface = self.setup_event_handler()

    def setup_event_handler(self) -> ops.charm.Object:
        """Configure event handlers for the relation.

        This method must be overridden in concrete class
        implementations.
        """
        raise NotImplementedError

    def get_interface(self) -> Tuple[ops.charm.Object, str]:
        """Return the interface that this handler encapsulates.

        This is a combination of the interface object and the
        name of the relation its wired into.
        """
        return self.interface, self.relation_name

    def interface_properties(self) -> dict:
        """Extract properties of the interface."""
        property_names = [
            p
            for p in dir(self.interface)
            if isinstance(getattr(type(self.interface), p, None), property)
        ]
        properties = {
            p: getattr(self.interface, p)
            for p in property_names
            if not p.startswith("_") and p not in ["model"]
        }
        return properties

    @property
    def ready(self) -> bool:
        """Determine with the relation is ready for use."""
        raise NotImplementedError

    def context(self) -> dict:
        """Pull together context for rendering templates."""
        return self.interface_properties()


class IngressHandler(RelationHandler):
    """Handler for Ingress relations."""

    def __init__(
        self,
        charm: ops.charm.CharmBase,
        relation_name: str,
        service_name: str,
        default_public_ingress_port: int,
        callback_f: Callable,
    ) -> None:
        """Run constructor."""
        self.default_public_ingress_port = default_public_ingress_port
        self.service_name = service_name
        super().__init__(charm, relation_name, callback_f)

    def setup_event_handler(self) -> ops.charm.Object:
        """Configure event handlers for an Ingress relation."""
        logger.debug("Setting up ingress event handler")
        interface = ingress.IngressRequires(self.charm, self.ingress_config)
        return interface

    @property
    def ingress_config(self) -> dict:
        """Ingress controller configuration dictionary."""
        # Most charms probably won't (or shouldn't) expose service-port
        # but use it if its there.
        port = self.model.config.get(
            "service-port", self.default_public_ingress_port
        )
        svc_hostname = self.model.config.get(
            "os-public-hostname", self.service_name
        )
        return {
            "service-hostname": svc_hostname,
            "service-name": self.charm.app.name,
            "service-port": port,
        }

    @property
    def ready(self) -> bool:
        """Whether the handler is ready for use."""
        # Nothing to wait for
        return True

    def context(self) -> dict:
        """Context containing ingress data."""
        return {}


class DBHandler(RelationHandler):
    """Handler for DB relations."""

    def __init__(
        self,
        charm: ops.charm.CharmBase,
        relation_name: str,
        callback_f: Callable,
        databases: List[str] = None,
    ) -> None:
        """Run constructor."""
        self.databases = databases
        super().__init__(charm, relation_name, callback_f)

    def setup_event_handler(self) -> ops.charm.Object:
        """Configure event handlers for a MySQL relation."""
        logger.debug("Setting up DB event handler")
        db = mysql.MySQLConsumer(
            self.charm, self.relation_name, databases=self.databases
        )
        _rname = self.relation_name.replace("-", "_")
        db_relation_event = getattr(
            self.charm.on, f"{_rname}_relation_changed"
        )
        self.framework.observe(db_relation_event, self._on_database_changed)
        return db

    def _on_database_changed(self, event: ops.framework.EventBase) -> None:
        """Handle database change events."""
        databases = self.interface.databases()
        logger.info(f"Received databases: {databases}")

        if not databases:
            return
        credentials = self.interface.credentials()
        # XXX Lets not log the credentials
        logger.info(f"Received credentials: {credentials}")
        self.callback_f(event)

    @property
    def ready(self) -> bool:
        """Whether the handler is ready for use."""
        try:
            # Nothing to wait for
            return bool(self.interface.databases())
        except AttributeError:
            return False

    def context(self) -> dict:
        """Context containing database connection data."""
        try:
            databases = self.interface.databases()
        except AttributeError:
            return {}
        if not databases:
            return {}
        ctxt = {
            "database": self.interface.databases()[0],
            "database_host": self.interface.credentials().get("address"),
            "database_password": self.interface.credentials().get("password"),
            "database_user": self.interface.credentials().get("username"),
            "database_type": "mysql+pymysql",
        }
        return ctxt


class AMQPHandler(RelationHandler):
    """Handler for managing a amqp relation."""

    DEFAULT_PORT = "5672"

    def __init__(
        self,
        charm: ops.charm.CharmBase,
        relation_name: str,
        callback_f: Callable,
        username: str,
        vhost: int,
    ) -> None:
        """Run constructor."""
        self.username = username
        self.vhost = vhost
        super().__init__(charm, relation_name, callback_f)

    def setup_event_handler(self) -> ops.charm.Object:
        """Configure event handlers for an AMQP relation."""
        logger.debug("Setting up AMQP event handler")
        amqp = sunbeam_amqp.AMQPRequires(
            self.charm, self.relation_name, self.username, self.vhost
        )
        self.framework.observe(amqp.on.ready, self._on_amqp_ready)
        return amqp

    def _on_amqp_ready(self, event: ops.framework.EventBase) -> None:
        """Handle AMQP change events."""
        # Ready is only emitted when the interface considers
        # that the relation is complete (indicated by a password)
        self.callback_f(event)

    @property
    def ready(self) -> bool:
        """Whether handler is ready for use."""
        try:
            return bool(self.interface.password)
        except AttributeError:
            return False

    def context(self) -> dict:
        """Context containing AMQP connection data."""
        try:
            hosts = self.interface.hostnames
        except AttributeError:
            return {}
        if not hosts:
            return {}
        ctxt = super().context()
        ctxt["hostnames"] = list(set(ctxt["hostnames"]))
        ctxt["hosts"] = ",".join(ctxt["hostnames"])
        ctxt["port"] = ctxt.get("ssl_port") or self.DEFAULT_PORT
        transport_url_hosts = ",".join(
            [
                "{}:{}@{}:{}".format(
                    self.username,
                    ctxt["password"],
                    host_,  # TODO deal with IPv6
                    ctxt["port"],
                )
                for host_ in ctxt["hostnames"]
            ]
        )
        transport_url = "rabbit://{}/{}".format(
            transport_url_hosts, self.vhost
        )
        ctxt["transport_url"] = transport_url
        return ctxt


class IdentityServiceRequiresHandler(RelationHandler):
    """Handler for managing a identity-service relation."""

    def __init__(
        self,
        charm: ops.charm.CharmBase,
        relation_name: str,
        callback_f: Callable,
        service_endpoints: dict,
        region: str,
    ) -> None:
        """Run constructor."""
        self.service_endpoints = service_endpoints
        self.region = region
        super().__init__(charm, relation_name, callback_f)

    def setup_event_handler(self) -> ops.charm.Object:
        """Configure event handlers for an Identity service relation."""
        logger.debug("Setting up Identity Service event handler")
        id_svc = sunbeam_id_svc.IdentityServiceRequires(
            self.charm, self.relation_name, self.service_endpoints, self.region
        )
        self.framework.observe(
            id_svc.on.ready, self._on_identity_service_ready
        )
        return id_svc

    def _on_identity_service_ready(
        self, event: ops.framework.EventBase
    ) -> None:
        """Handle AMQP change events."""
        # Ready is only emitted when the interface considers
        # that the relation is complete (indicated by a password)
        self.callback_f(event)

    @property
    def ready(self) -> bool:
        """Whether handler is ready for use."""
        try:
            return bool(self.interface.service_password)
        except AttributeError:
            return False


class BasePeerHandler(RelationHandler):
    """Base handler for managing a peers relation."""

    LEADER_READY_KEY = "leader_ready"

    def setup_event_handler(self) -> None:
        """Configure event handlers for peer relation."""
        logger.debug("Setting up peer event handler")
        peer_int = sunbeam_interfaces.OperatorPeers(
            self.charm,
            self.relation_name,
        )
        self.framework.observe(
            peer_int.on.peers_data_changed, self._on_peers_data_changed
        )
        return peer_int

    def _on_peers_data_changed(self, event: ops.framework.EventBase) -> None:
        """Process peer data changed event."""
        self.callback_f(event)

    @property
    def ready(self) -> bool:
        """Whether the handler is complete."""
        return True

    def context(self) -> dict:
        """Return all app data set on the peer relation."""
        try:
            return self.interface.get_all_app_data()
        except AttributeError:
            return {}

    def set_app_data(self, settings: dict) -> None:
        """Store data in peer app db."""
        self.interface.set_app_data(settings)

    def get_app_data(self, key: str) -> str:
        """Retrieve data from the peer relation."""
        return self.interface.get_app_data(key)

    def leader_get(self, key: str) -> str:
        """Retrieve data from the peer relation."""
        return self.peers.get_app_data(key)

    def leader_set(self, settings: dict, **kwargs) -> None:
        """Store data in peer app db."""
        settings = settings or {}
        settings.update(kwargs)
        self.set_app_data(settings)

    def set_leader_ready(self) -> None:
        """Tell peers the leader is ready."""
        self.set_app_data({self.LEADER_READY_KEY: json.dumps(True)})

    def is_leader_ready(self) -> bool:
        """Whether the leader has announced it is ready."""
        ready = self.get_app_data(self.LEADER_READY_KEY)
        if ready is None:
            return False
        else:
            return json.loads(ready)


class CephClientHandler(RelationHandler):
    """Handler for ceph-client interface."""

    def __init__(
        self,
        charm: ops.charm.CharmBase,
        relation_name: str,
        callback_f: Callable,
        allow_ec_overwrites: bool = True,
        app_name: str = None
    ) -> None:
        """Run constructor."""
        self.allow_ec_overwrites = allow_ec_overwrites
        self.app_name = app_name
        super().__init__(charm, relation_name, callback_f)

    def setup_event_handler(self) -> ops.charm.Object:
        """Configure event handlers for an ceph-client interface."""
        logger.debug("Setting up ceph-client event handler")
        ceph = ceph_client.CephClientRequires(
            self.charm,
            self.relation_name,
        )
        self.framework.observe(
            ceph.on.pools_available, self._on_pools_available
        )
        self.framework.observe(
            ceph.on.broker_available, self.request_pools
        )
        return ceph

    def _on_pools_available(self, event: ops.framework.EventBase) -> None:
        """Handle pools available event."""
        # Ready is only emitted when the interface considers
        # that the relation is complete
        self.callback_f(event)

    def request_pools(self, event: ops.framework.EventBase) -> None:
        """
        Request Ceph pool creation when interface broker is ready.

        The default handler will automatically request erasure-coded
        or replicated pools depending on the configuration of the
        charm from which the handler is being used.

        To provide charm specific behaviour, subclass the default
        handler and use the required broker methods on the underlying
        interface object.
        """
        config = self.model.config.get
        data_pool_name = (
            config("rbd-pool-name") or
            config("rbd-pool") or
            self.charm.app.name
        )
        metadata_pool_name = (
            config("ec-rbd-metadata-pool") or f"{self.charm.app.name}-metadata"
        )
        weight = config("ceph-pool-weight")
        replicas = config("ceph-osd-replication-count")
        # TODO: add bluestore compression options
        if config("pool-type") == ERASURE_CODED:
            # General EC plugin config
            plugin = config("ec-profile-plugin")
            technique = config("ec-profile-technique")
            device_class = config("ec-profile-device-class")
            bdm_k = config("ec-profile-k")
            bdm_m = config("ec-profile-m")
            # LRC plugin config
            bdm_l = config("ec-profile-locality")
            crush_locality = config("ec-profile-crush-locality")
            # SHEC plugin config
            bdm_c = config("ec-profile-durability-estimator")
            # CLAY plugin config
            bdm_d = config("ec-profile-helper-chunks")
            scalar_mds = config("ec-profile-scalar-mds")
            # Profile name
            profile_name = (
                config("ec-profile-name") or f"{self.charm.app.name}-profile"
            )
            # Metadata sizing is approximately 1% of overall data weight
            # but is in effect driven by the number of rbd's rather than
            # their size - so it can be very lightweight.
            metadata_weight = weight * 0.01
            # Resize data pool weight to accomodate metadata weight
            weight = weight - metadata_weight
            # Create erasure profile
            self.interface.create_erasure_profile(
                name=profile_name,
                k=bdm_k,
                m=bdm_m,
                lrc_locality=bdm_l,
                lrc_crush_locality=crush_locality,
                shec_durability_estimator=bdm_c,
                clay_helper_chunks=bdm_d,
                clay_scalar_mds=scalar_mds,
                device_class=device_class,
                erasure_type=plugin,
                erasure_technique=technique,
            )

            # Create EC data pool
            self.interface.create_erasure_pool(
                name=data_pool_name,
                erasure_profile=profile_name,
                weight=weight,
                allow_ec_overwrites=self.allow_ec_overwrites,
                app_name=self.app_name,
            )
            # Create EC metadata pool
            self.interface.create_replicated_pool(
                name=metadata_pool_name,
                replicas=replicas,
                weight=metadata_weight,
                app_name=self.app_name,
            )
        else:
            self.interface.create_replicated_pool(
                name=data_pool_name, replicas=replicas, weight=weight,
                app_name=self.app_name,
            )

    @property
    def ready(self) -> bool:
        """Whether handler ready for use."""
        return self.interface.pools_available

    @property
    def key(self) -> str:
        """Retrieve the cephx key provided for the application."""
        return self.interface.get_relation_data().get('key')

    def context(self) -> dict:
        """Context containing Ceph connection data."""
        ctxt = super().context()
        data = self.interface.get_relation_data()
        ctxt['mon_hosts'] = ",".join(
            sorted(data.get("mon_hosts"))
        )
        ctxt['auth'] = data.get('auth')
        ctxt['key'] = data.get("key")
        ctxt['rbd_features'] = None
        return ctxt