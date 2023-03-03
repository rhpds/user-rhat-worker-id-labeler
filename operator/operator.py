import kopf
import kubernetes_asyncio
import logging
import os
import re

from configure_kopf_logging import configure_kopf_logging
from infinite_relative_backoff import InfiniteRelativeBackoff

core_v1_api = custom_objects_api = operator_namespace = None

name_with_rhatWorkerId_re = re.compile(r' \[(\d+)\]$')

class User:
    @staticmethod
    async def get(name):
        definition = await custom_objects_api.get_cluster_custom_object(
            'user.openshift.io', 'v1', 'users', name
        )
        return User(definition)

    def __init__(self, definition):
        self.fullName = definition.get('fullName')
        self.identities = definition.get('identities')
        self.metadata = definition['metadata']

    def __str__(self):
        return f"User {self.name}"

    @property
    def has_sso_internal_identity_provider(self):
        for identity in self.identities:
            if identity.startswith('sso-internal:'):
                return True
        return False

    @property
    def name(self):
        return self.metadata['name']

    async def manage_rhatWorkerId_label(self, logger):
        if not self.has_sso_internal_identity_provider \
        or not self.fullName:
            return
        match = name_with_rhatWorkerId_re.search(self.fullName)
        if not match:
            return

        fullName = self.fullName[:-len(match.group(0))]
        rhatWorkerId = match.group(1)

        logger.info(f"Patching {self} with redhat.com/rhatWorkerId {rhatWorkerId}")

        await custom_objects_api.patch_cluster_custom_object(
            group = 'user.openshift.io',
            name = self.name,
            plural = 'users',
            version = 'v1',
            body = {
                "fullName": fullName,
                "metadata": {
                    "labels": {
                        "redhat.com/rhatWorkerId": rhatWorkerId,
                    }
                }
            }
        )


@kopf.on.startup()
async def configure(settings: kopf.OperatorSettings, **_):
    global core_v1_api, custom_objects_api

    # Never give up from network errors
    settings.networking.error_backoffs = InfiniteRelativeBackoff()

    # Only create events for warnings and errors
    settings.posting.level = logging.WARNING

    # Disable scanning for CustomResourceDefinitions updates
    settings.scanning.disabled = True

    # Configure logging
    configure_kopf_logging()

    if os.path.exists('/run/secrets/kubernetes.io/serviceaccount/token'):
        kubernetes_asyncio.config.load_incluster_config()
    else:
        await kubernetes_asyncio.config.load_kube_config()

    core_v1_api = kubernetes_asyncio.client.CoreV1Api()
    custom_objects_api = kubernetes_asyncio.client.CustomObjectsApi()


@kopf.on.event('user.openshift.io', 'v1', 'users')
async def user_handler(event, logger, **_):
    user = User(event['object'])
    if event['type'] != 'DELETED':
        await user.manage_rhatWorkerId_label(logger=logger)
