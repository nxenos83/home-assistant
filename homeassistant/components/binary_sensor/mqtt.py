"""
Support for MQTT binary sensors.

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/binary_sensor.mqtt/
"""
import logging

import voluptuous as vol

from homeassistant.core import callback
from homeassistant.components import mqtt, binary_sensor
from homeassistant.components.binary_sensor import (
    BinarySensorDevice, DEVICE_CLASSES_SCHEMA)
from homeassistant.const import (
    CONF_FORCE_UPDATE, CONF_NAME, CONF_VALUE_TEMPLATE, CONF_PAYLOAD_ON,
    CONF_PAYLOAD_OFF, CONF_DEVICE_CLASS, CONF_DEVICE)
from homeassistant.components.mqtt import (
    ATTR_DISCOVERY_HASH, CONF_STATE_TOPIC, CONF_AVAILABILITY_TOPIC,
    CONF_PAYLOAD_AVAILABLE, CONF_PAYLOAD_NOT_AVAILABLE, CONF_QOS,
    MqttAvailability, MqttDiscoveryUpdate, MqttEntityDeviceInfo,
    subscription)
from homeassistant.components.mqtt.discovery import MQTT_DISCOVERY_NEW
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_connect
import homeassistant.helpers.event as evt
from homeassistant.helpers.typing import HomeAssistantType, ConfigType

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = 'MQTT Binary sensor'
CONF_OFF_DELAY = 'off_delay'
CONF_UNIQUE_ID = 'unique_id'
DEFAULT_PAYLOAD_OFF = 'OFF'
DEFAULT_PAYLOAD_ON = 'ON'
DEFAULT_FORCE_UPDATE = False

DEPENDENCIES = ['mqtt']

PLATFORM_SCHEMA = mqtt.MQTT_RO_PLATFORM_SCHEMA.extend({
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Optional(CONF_PAYLOAD_OFF, default=DEFAULT_PAYLOAD_OFF): cv.string,
    vol.Optional(CONF_PAYLOAD_ON, default=DEFAULT_PAYLOAD_ON): cv.string,
    vol.Optional(CONF_DEVICE_CLASS): DEVICE_CLASSES_SCHEMA,
    vol.Optional(CONF_FORCE_UPDATE, default=DEFAULT_FORCE_UPDATE): cv.boolean,
    vol.Optional(CONF_OFF_DELAY):
        vol.All(vol.Coerce(int), vol.Range(min=0)),
    # Integrations shouldn't never expose unique_id through configuration
    # this here is an exception because MQTT is a msg transport, not a protocol
    vol.Optional(CONF_UNIQUE_ID): cv.string,
    vol.Optional(CONF_DEVICE): mqtt.MQTT_ENTITY_DEVICE_INFO_SCHEMA,
}).extend(mqtt.MQTT_AVAILABILITY_SCHEMA.schema)


async def async_setup_platform(hass: HomeAssistantType, config: ConfigType,
                               async_add_entities, discovery_info=None):
    """Set up MQTT binary sensor through configuration.yaml."""
    await _async_setup_entity(hass, config, async_add_entities)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up MQTT binary sensor dynamically through MQTT discovery."""
    async def async_discover(discovery_payload):
        """Discover and add a MQTT binary sensor."""
        config = PLATFORM_SCHEMA(discovery_payload)
        await _async_setup_entity(hass, config, async_add_entities,
                                  discovery_payload[ATTR_DISCOVERY_HASH])

    async_dispatcher_connect(
        hass, MQTT_DISCOVERY_NEW.format(binary_sensor.DOMAIN, 'mqtt'),
        async_discover)


async def _async_setup_entity(hass, config, async_add_entities,
                              discovery_hash=None):
    """Set up the MQTT binary sensor."""
    value_template = config.get(CONF_VALUE_TEMPLATE)
    if value_template is not None:
        value_template.hass = hass

    async_add_entities([MqttBinarySensor(
        config,
        discovery_hash
    )])


class MqttBinarySensor(MqttAvailability, MqttDiscoveryUpdate,
                       MqttEntityDeviceInfo, BinarySensorDevice):
    """Representation a binary sensor that is updated by MQTT."""

    def __init__(self, config, discovery_hash):
        """Initialize the MQTT binary sensor."""
        self._config = config
        self._state = None
        self._sub_state = None
        self._delay_listener = None

        self._name = None
        self._state_topic = None
        self._device_class = None
        self._payload_on = None
        self._payload_off = None
        self._qos = None
        self._force_update = None
        self._off_delay = None
        self._template = None
        self._unique_id = None

        # Load config
        self._setup_from_config(config)

        availability_topic = config.get(CONF_AVAILABILITY_TOPIC)
        payload_available = config.get(CONF_PAYLOAD_AVAILABLE)
        payload_not_available = config.get(CONF_PAYLOAD_NOT_AVAILABLE)
        device_config = config.get(CONF_DEVICE)

        MqttAvailability.__init__(self, availability_topic, self._qos,
                                  payload_available, payload_not_available)
        MqttDiscoveryUpdate.__init__(self, discovery_hash,
                                     self.discovery_update)
        MqttEntityDeviceInfo.__init__(self, device_config)

    async def async_added_to_hass(self):
        """Subscribe mqtt events."""
        await MqttAvailability.async_added_to_hass(self)
        await MqttDiscoveryUpdate.async_added_to_hass(self)
        await self._subscribe_topics()

    async def discovery_update(self, discovery_payload):
        """Handle updated discovery message."""
        config = PLATFORM_SCHEMA(discovery_payload)
        self._setup_from_config(config)
        await self.availability_discovery_update(config)
        await self._subscribe_topics()
        self.async_schedule_update_ha_state()

    def _setup_from_config(self, config):
        """(Re)Setup the entity."""
        self._name = config.get(CONF_NAME)
        self._state_topic = config.get(CONF_STATE_TOPIC)
        self._device_class = config.get(CONF_DEVICE_CLASS)
        self._qos = config.get(CONF_QOS)
        self._force_update = config.get(CONF_FORCE_UPDATE)
        self._off_delay = config.get(CONF_OFF_DELAY)
        self._payload_on = config.get(CONF_PAYLOAD_ON)
        self._payload_off = config.get(CONF_PAYLOAD_OFF)
        value_template = config.get(CONF_VALUE_TEMPLATE)
        if value_template is not None and value_template.hass is None:
            value_template.hass = self.hass
        self._template = value_template

        self._unique_id = config.get(CONF_UNIQUE_ID)

    async def _subscribe_topics(self):
        """(Re)Subscribe to topics."""
        @callback
        def off_delay_listener(now):
            """Switch device off after a delay."""
            self._delay_listener = None
            self._state = False
            self.async_schedule_update_ha_state()

        @callback
        def state_message_received(_topic, payload, _qos):
            """Handle a new received MQTT state message."""
            if self._template is not None:
                payload = self._template.async_render_with_possible_json_value(
                    payload)
            if payload == self._payload_on:
                self._state = True
            elif payload == self._payload_off:
                self._state = False
            else:  # Payload is not for this entity
                _LOGGER.warning('No matching payload found'
                                ' for entity: %s with state_topic: %s',
                                self._name, self._state_topic)
                return

            if self._delay_listener is not None:
                self._delay_listener()
                self._delay_listener = None

            if (self._state and self._off_delay is not None):
                self._delay_listener = evt.async_call_later(
                    self.hass, self._off_delay, off_delay_listener)

            self.async_schedule_update_ha_state()

        self._sub_state = await subscription.async_subscribe_topics(
            self.hass, self._sub_state,
            {'state_topic': {'topic': self._state_topic,
                             'msg_callback': state_message_received,
                             'qos': self._qos}})

    async def async_will_remove_from_hass(self):
        """Unsubscribe when removed."""
        await subscription.async_unsubscribe_topics(self.hass, self._sub_state)
        await MqttAvailability.async_will_remove_from_hass(self)

    @property
    def should_poll(self):
        """Return the polling state."""
        return False

    @property
    def name(self):
        """Return the name of the binary sensor."""
        return self._name

    @property
    def is_on(self):
        """Return true if the binary sensor is on."""
        return self._state

    @property
    def device_class(self):
        """Return the class of this sensor."""
        return self._device_class

    @property
    def force_update(self):
        """Force update."""
        return self._force_update

    @property
    def unique_id(self):
        """Return a unique ID."""
        return self._unique_id
