import logging

from simplejson import loads, load
from tb_rest_client.rest import ApiException
from tb_rest_client.rest_client_ce import *

LOG = logging.getLogger("TEST")


class GatewayDeviceUtil:
    DEFAULT_URL = "http://127.0.0.1:8080"

    DEFAULT_USERNAME = "tenant@thingsboard.org"
    DEFAULT_PASSWORD = "tenant"

    GATEWAY_DEVICE_NAME = "Test Gateway device"
    GATEWAY_DEVICE = None

    GATEWAY_ACCESS_TOKEN = "YOUR_ACCESS_TOKEN"

    @classmethod
    def get_gateway_device(cls, url=DEFAULT_URL, username=DEFAULT_USERNAME, password=DEFAULT_PASSWORD) -> Device:
        if cls.GATEWAY_DEVICE is None:
            cls.create_gateway_device(url=url, username=username, password=password)
        return cls.GATEWAY_DEVICE

    @staticmethod
    def create_gateway_device(url=DEFAULT_URL, username=DEFAULT_USERNAME, password=DEFAULT_PASSWORD) -> Device:
        with RestClientCE(base_url=url) as rest_client:
            try:
                rest_client.login(username=username, password=password)
                gateway_device_profile = rest_client.get_default_device_profile_info()
                additional_info = {"gateway": True,
                                   "overwriteActivityTime": False,
                                   "description": ""}
                gateway_device = Device(name=GatewayDeviceUtil.GATEWAY_DEVICE_NAME,
                                        device_profile_id=gateway_device_profile.id,
                                        additional_info=additional_info)
                gateway_device = rest_client.save_device(gateway_device,
                                                         access_token=GatewayDeviceUtil.GATEWAY_ACCESS_TOKEN)
                GatewayDeviceUtil.GATEWAY_DEVICE = gateway_device

                LOG.info("Gateway device was created: %r", gateway_device.name)

                return gateway_device
            except ApiException as e:
                response_body = loads(bytes.decode(e.body, encoding='UTF-8'))
                if response_body:
                    if not response_body.get("status") == 400 or not response_body.get(
                            "message") == "Device with such name already exists!":
                        LOG.exception(e)
                        exit(1)
                    else:
                        LOG.info("Gateway device already exists: %r", GatewayDeviceUtil.GATEWAY_DEVICE_NAME)
                        gateway_device = rest_client.get_tenant_device(GatewayDeviceUtil.GATEWAY_DEVICE_NAME)
                        GatewayDeviceUtil.GATEWAY_DEVICE = gateway_device
                        return gateway_device

    @staticmethod
    def delete_gateway_device():
        if GatewayDeviceUtil.GATEWAY_DEVICE is None:
            return

        with RestClientCE(base_url=GatewayDeviceUtil.DEFAULT_URL) as rest_client:
            try:
                rest_client.login(username=GatewayDeviceUtil.DEFAULT_USERNAME,
                                  password=GatewayDeviceUtil.DEFAULT_PASSWORD)
                rest_client.delete_device(GatewayDeviceUtil.GATEWAY_DEVICE.id)
                LOG.info("Gateway device was deleted: %r", GatewayDeviceUtil.GATEWAY_DEVICE.name)
                GatewayDeviceUtil.GATEWAY_DEVICE = None
            except ApiException as e:
                LOG.exception(e)
                exit(1)

    @staticmethod
    def delete_device(device_id):
        with RestClientCE(base_url=GatewayDeviceUtil.DEFAULT_URL) as rest_client:
            try:
                rest_client.login(username=GatewayDeviceUtil.DEFAULT_USERNAME,
                                  password=GatewayDeviceUtil.DEFAULT_PASSWORD)
                rest_client.delete_device(device_id)
                LOG.info("Device was deleted: %r", device_id)
            except ApiException as e:
                LOG.exception(e)
                exit(1)

    @classmethod
    def load_configuration(cls, config_file_path):
        with open(config_file_path, 'r', encoding="UTF-8") as config:
            config = load(config)
        return config

    @classmethod
    def update_connector_config(cls, connector_name, config_file_path):
        with RestClientCE(base_url=GatewayDeviceUtil.DEFAULT_URL) as rest_client:
            rest_client.login(username=GatewayDeviceUtil.DEFAULT_USERNAME,
                              password=GatewayDeviceUtil.DEFAULT_PASSWORD)
            config = cls.load_configuration(config_file_path)
            config[connector_name]['ts'] = int(time() * 1000)
            response = rest_client.save_device_attributes(cls.GATEWAY_DEVICE.id, 'SHARED_SCOPE', config)
            sleep(3)
            return config, response

    @classmethod
    def is_gateway_connected(cls):
        """
        Check if the gateway is connected.

        Returns:
            bool: True if the gateway is connected, False otherwise.
        """
        with RestClientCE(base_url=GatewayDeviceUtil.DEFAULT_URL) as rest_client:
            try:
                rest_client.login(username=GatewayDeviceUtil.DEFAULT_USERNAME,
                                  password=GatewayDeviceUtil.DEFAULT_PASSWORD)
                return rest_client.get_attributes_by_scope(cls.GATEWAY_DEVICE.id, 'SERVER_SCOPE', 'active')[0]['value']
            except IndexError:
                return False

    @classmethod
    def restart_gateway(cls):
        with RestClientCE(base_url=GatewayDeviceUtil.DEFAULT_URL) as rest_client:
            rest_client.login(username=GatewayDeviceUtil.DEFAULT_USERNAME,
                              password=GatewayDeviceUtil.DEFAULT_PASSWORD)
            rest_client.handle_two_way_device_rpc_request(cls.GATEWAY_DEVICE.id, {"method": "gateway_restart"})
            sleep(5)

        while not cls.is_gateway_connected():
            LOG.info('Gateway connecting to TB...')
            sleep(1)
