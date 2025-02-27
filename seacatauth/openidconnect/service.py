import datetime
import json
import os.path
import base64
import secrets
import logging

import asab

import aiohttp.web
import urllib.parse
import jwcrypto.jwt
import jwcrypto.jwk
import jwcrypto.jws

from ..session import SessionAdapter
from ..session import (
	credentials_session_builder,
	authz_session_builder,
	cookie_session_builder,
	login_descriptor_session_builder,
)
from .session import oauth2_session_builder

#

L = logging.getLogger(__name__)

#

# TODO: Use JWA algorithms?


class OpenIdConnectService(asab.Service):

	# Bearer token Regex is based on RFC 6750
	# The OAuth 2.0 Authorization Framework: Bearer Token Usage
	# Chapter 2.1. Authorization Request Header Field
	AuthorizationCodeCollection = "ac"

	def __init__(self, app, service_name="seacatauth.OpenIdConnectService"):
		super().__init__(app, service_name)
		self.StorageService = app.get_service("asab.StorageService")
		self.SessionService = app.get_service("seacatauth.SessionService")
		self.CredentialsService = app.get_service("seacatauth.CredentialsService")
		self.TenantService = app.get_service("seacatauth.TenantService")
		self.RoleService = app.get_service("seacatauth.RoleService")
		self.AuditService = app.get_service("seacatauth.AuditService")

		self.BearerRealm = asab.Config.get("openidconnect", "bearer_realm")
		self.Issuer = asab.Config.get("openidconnect", "issuer", fallback=None)
		if self.Issuer is None:
			fragments = urllib.parse.urlparse(asab.Config.get("general", "auth_webui_base_url"))
			L.warning("OAuth2 issuer not specified. Assuming '{}'".format(fragments.netloc))
			self.Issuer = fragments.netloc

		self.AuthorizationCodeTimeout = datetime.timedelta(
			seconds=asab.Config.getseconds("openidconnect", "auth_code_timeout")
		)

		self.PrivateKey = self._load_private_key()

		self.App.PubSub.subscribe("Application.tick/60!", self._on_tick)


	async def _on_tick(self, event_name):
		await self.delete_expired_authorization_codes()


	def _load_private_key(self):
		"""
		Load private key from file.
		If it does not exist, generate a new one and write to file.
		"""
		# TODO: Add encryption option
		# TODO: Multiple key support
		private_key_path = asab.Config.get("openidconnect", "private_key")
		if len(private_key_path) == 0:
			# Use config folder
			private_key_path = os.path.join(
				os.path.dirname(asab.Config.get("general", "config_file")),
				"private-key.pem"
			)
			L.log(
				asab.LOG_NOTICE,
				"OpenIDConnect private key file not specified. Defaulting to '{}'.".format(private_key_path)
			)

		if os.path.isfile(private_key_path):
			with open(private_key_path, "rb") as f:
				private_key = jwcrypto.jwk.JWK.from_pem(f.read())
		elif self.App.Provisioning:
			# Generate a new private key
			L.warning(
				"OpenIDConnect private key file does not exist. Generating a new one."
			)
			private_key = self._generate_private_key(private_key_path)
		else:
			raise FileNotFoundError(
				"Private key file '{}' does not exist. "
				"Run the app in provisioning mode to generate a new private key.".format(private_key_path)
			)

		assert private_key.key_type == "EC"
		assert private_key.key_curve == "P-256"
		return private_key


	def _generate_private_key(self, private_key_path):
		assert not os.path.isfile(private_key_path)

		import cryptography.hazmat.backends
		import cryptography.hazmat.primitives.serialization
		import cryptography.hazmat.primitives.asymmetric.ec
		import cryptography.hazmat.primitives.ciphers.algorithms
		_private_key = cryptography.hazmat.primitives.asymmetric.ec.generate_private_key(
			cryptography.hazmat.primitives.asymmetric.ec.SECP256R1(),
			cryptography.hazmat.backends.default_backend()
		)
		# Serialize into PEM
		private_pem = _private_key.private_bytes(
			encoding=cryptography.hazmat.primitives.serialization.Encoding.PEM,
			format=cryptography.hazmat.primitives.serialization.PrivateFormat.PKCS8,
			encryption_algorithm=cryptography.hazmat.primitives.serialization.NoEncryption()
		)
		with open(private_key_path, "wb") as f:
			f.write(private_pem)
		L.log(
			asab.LOG_NOTICE,
			"New private key written to '{}'.".format(private_key_path)
		)
		private_key = jwcrypto.jwk.JWK.from_pem(private_pem)
		return private_key


	async def generate_authorization_code(self, session_id):
		code = secrets.token_urlsafe(36)
		upsertor = self.StorageService.upsertor(self.AuthorizationCodeCollection, code)

		upsertor.set("sid", session_id)
		upsertor.set("exp", datetime.datetime.now(datetime.timezone.utc) + self.AuthorizationCodeTimeout)

		await upsertor.execute()

		return code


	async def delete_expired_authorization_codes(self):
		collection = self.StorageService.Database[self.AuthorizationCodeCollection]

		query_filter = {"exp": {"$lt": datetime.datetime.now(datetime.timezone.utc)}}
		result = await collection.delete_many(query_filter)
		if result.deleted_count > 0:
			L.info("Expired login sessions deleted", struct_data={
				"count": result.deleted_count
			})


	async def pop_session_id_by_authorization_code(self, code):
		collection = self.StorageService.Database[self.AuthorizationCodeCollection]
		data = await collection.find_one_and_delete(filter={"_id": code})
		if data is None:
			raise KeyError("Authorization code not found")

		session_id = data["sid"]
		exp = data["exp"]
		if exp is None or exp < datetime.datetime.now(datetime.timezone.utc):
			raise KeyError("Authorization code expired")

		return session_id


	async def get_session_by_access_token(self, token_value):
		# Decode the access token
		try:
			access_token = base64.urlsafe_b64decode(token_value)
		except ValueError:
			L.info("Access token is not base64: '{}'".format(token_value))
			return None

		# Locate the session
		try:
			session = await self.SessionService.get_by(SessionAdapter.FN.OAuth2.AccessToken, access_token)
		except KeyError:
			return None

		return session


	def build_session_from_id_token(self, token_value):
		try:
			token = jwcrypto.jwt.JWT(jwt=token_value, key=self.PrivateKey)
		except jwcrypto.jwt.JWTExpired:
			L.warning("ID token expired")
			return None
		except jwcrypto.jws.InvalidJWSSignature:
			L.warning("Invalid ID token signature")
			return None

		try:
			data_dict = json.loads(token.claims)
		except ValueError:
			L.warning("Cannot read ID token claims")
			return None

		try:
			session = SessionAdapter.from_id_token(self.SessionService, data_dict)
		except ValueError:
			L.warning("Cannot build session from ID token data")
			return None

		return session


	def refresh_token(self, refresh_token, client_id, client_secret, scope):
		# TODO: this is not implemented
		L.error("refresh_token is not implemented", struct_data=[refresh_token, client_id, client_secret, scope])
		raise aiohttp.web.HTTPNotImplemented()


	def check_access_token(self, bearer_token):
		# TODO: this is not implemented
		L.error("check_access_token is not implemented", struct_data={"bearer": bearer_token})
		raise aiohttp.web.HTTPNotImplemented()


	async def create_oidc_session(self, root_session, client_id, scope, requested_expiration=None):
		# TODO: Choose builders based on scope
		session_builders = [
			await credentials_session_builder(self.CredentialsService, root_session.Credentials.Id),
			await authz_session_builder(
				tenant_service=self.TenantService,
				role_service=self.RoleService,
				credentials_id=root_session.Credentials.Id
			),
			login_descriptor_session_builder(root_session.Authentication.LoginDescriptor),
			cookie_session_builder(),
		]

		# TODO: if 'openid' in scope
		oauth2_data = {
			"scope": scope,
			"client_id": client_id,
		}
		session_builders.append(oauth2_session_builder(oauth2_data))
		session = await self.SessionService.create_session(
			session_type="openidconnect",
			parent_session=root_session,
			expiration=requested_expiration,
			session_builders=session_builders,
		)

		return session


	async def build_userinfo(self, session, tenant=None):
		userinfo = {
			"iss": self.Issuer,
			"sub": session.Credentials.Id,  # The sub (subject) Claim MUST always be returned in the UserInfo Response.
			"exp": session.Session.Expiration,
			"iat": datetime.datetime.now(datetime.timezone.utc),
		}

		if session.OAuth2.ClientId is not None:
			# aud indicates who is allowed to consume the token
			# azp indicates who is allowed to present it
			userinfo["aud"] = session.OAuth2.ClientId
			userinfo["azp"] = session.OAuth2.ClientId

		if session.Credentials.Username is not None:
			userinfo["preferred_username"] = session.Credentials.Username

		if session.Credentials.Email is not None:
			userinfo["email"] = session.Credentials.Email

		if session.Credentials.Phone is not None:
			userinfo["phone_number"] = session.Credentials.Phone

		if session.Credentials.ModifiedAt is not None:
			userinfo["updated_at"] = session.Credentials.ModifiedAt

		if session.Credentials.CreatedAt is not None:
			userinfo["created_at"] = session.Credentials.CreatedAt

		if session.Authentication.TOTPSet is not None:
			userinfo["totp_set"] = session.Authentication.TOTPSet

		if session.Authentication.AvailableFactors is not None:
			userinfo["available_factors"] = session.Authentication.AvailableFactors

		if session.Authentication.LoginDescriptor is not None:
			userinfo["ldid"] = session.Authentication.LoginDescriptor["id"]
			userinfo["factors"] = [
				factor["type"]
				for factor
				in session.Authentication.LoginDescriptor["factors"]
			]

		# List enabled external login providers
		if session.Authentication.ExternalLoginOptions is not None:
			userinfo["external_login_enabled"] = [
				account_type
				for account_type, account_id in session.Authentication.ExternalLoginOptions.items()
				if len(account_id) > 0
			]

		if session.Authorization.Authz is not None:
			userinfo["authz"] = session.Authorization.Authz

		if session.Authorization.Authz is not None:
			# Include the list of ALL the user's tenants (excluding "*")
			tenants = [t for t in session.Authorization.Authz.keys() if t != "*"]
			if len(tenants) > 0:
				userinfo["tenants"] = tenants

		if session.Authorization.Resources is not None:
			userinfo["resources"] = session.Authorization.Resources

		if session.Authorization.Tenants is not None:
			userinfo["tenants"] = session.Authorization.Tenants

		# TODO: Last password change

		# Get last successful and failed login times
		# TODO: Store last login in session
		try:
			last_login = await self.AuditService.get_last_logins(session.Credentials.Id)
		except Exception as e:
			last_login = None
			L.warning("Could not fetch last logins: {}".format(e))

		if last_login is not None:
			if "fat" in last_login:
				userinfo["last_failed_login"] = last_login["fat"]
			if "sat" in last_login:
				userinfo["last_successful_login"] = last_login["sat"]

		# If tenant is missing or unknown, consider only global roles and resources
		if tenant not in session.Authorization.Authz:
			L.warning("Request for unknown tenant '{}', defaulting to '*'.".format(tenant))
			tenant = "*"

		# Include "roles" and "resources" sections, with items relevant to query_tenant
		resources = session.Authorization.Authz.get(tenant)
		if resources is not None:
			userinfo["resources"] = resources
		else:
			L.error(
				"Tenant '{}' not found in session.Authorization.authz.".format(tenant),
				struct_data={
					"sid": session.SessionId,
					"cid": session.Credentials.Id,
					"authz": session.Authorization.Authz.keys()
				}
			)

		# RFC 7519 states that the exp and iat claim values must be NumericDate values
		# Convert ALL datetimes to UTC timestamps for consistency
		for k, v in userinfo.items():
			if isinstance(v, datetime.datetime):
				userinfo[k] = int(v.timestamp())

		return userinfo
