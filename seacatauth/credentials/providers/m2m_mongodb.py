import logging
from typing import Optional

import asab.storage.exceptions

import pymongo

from .mongodb import MongoDBCredentialsProvider

#

L = logging.getLogger(__name__)

#


class M2MMongoDBCredentialsService(asab.Service):

	def __init__(self, app, service_name="seacatauth.credentials.m2m"):
		super().__init__(app, service_name)

	def create_provider(self, provider_id, config_section_name):
		return M2MMongoDBCredentialsProvider(self.App, provider_id, config_section_name)


class M2MMongoDBCredentialsProvider(MongoDBCredentialsProvider):
	"""
	Machine credentials provider with MongoDB backend.

	Machine credentials are meant solely for machine-to-machine communication (API access)
	and cannot be used for web UI login.
	No registration.
	No ident (basic auth must be exact username match)

	Available authn factors:
	basic_auth (username+password)
	api_token
	certificate
	"""
	# TODO: Implement API key authn
	# TODO: Implement certificate authn

	Type = "m2m"

	ConfigDefaults = {
		"credentials_collection": "mc",
		"tenants": "no",
		"creation_features": "username password",
		"ident_fields": "username"
	}

	def __init__(self, app, provider_id, config_section_name):
		super().__init__(app, provider_id, config_section_name)
		self.RegistrationFeatures = None

	async def initialize(self):
		coll = await self.MongoDBStorageService.collection(self.CredentialsCollection)

		try:
			await coll.create_index(
				[
					("username", pymongo.ASCENDING),
				],
				unique=True
			)
		except Exception as e:
			L.warning("{}; fix it and restart the app".format(e))

	async def register(self, register_info: dict) -> Optional[str]:
		return None

	async def get_login_descriptors(self, credentials_id):
		return None
