import logging
import secrets

from .adapter import SessionAdapter
from ..authz import get_credentials_authz

#

L = logging.getLogger(__name__)

#


async def credentials_session_builder(credentials_service, credentials_id):
	credentials = await credentials_service.get(credentials_id, include=["__totp"])
	return (
		(SessionAdapter.FN.Credentials.Id, credentials_id),
		(SessionAdapter.FN.Credentials.Username, credentials.get("username")),
		(SessionAdapter.FN.Credentials.Email, credentials.get("email")),
		(SessionAdapter.FN.Credentials.Phone, credentials.get("phone")),
		(SessionAdapter.FN.Credentials.CreatedAt, credentials.get("_c")),
		(SessionAdapter.FN.Credentials.ModifiedAt, credentials.get("_m")),
		(SessionAdapter.FN.Authentication.ExternalLoginOptions, credentials.get("external_login")),
		(SessionAdapter.FN.Authentication.TOTPSet, credentials.get("__totp") not in (None, "")),
	)


async def authz_session_builder(tenant_service, role_service, credentials_id):
	"""
	Add 'authz' dict with complete information about credentials' tenants, roles and resources
	"""
	return ((SessionAdapter.FN.Authorization.Authz, await get_credentials_authz(credentials_id, tenant_service, role_service)),)


def login_descriptor_session_builder(login_descriptor):
	if login_descriptor is not None:
		yield (SessionAdapter.FN.Authentication.LoginDescriptor, login_descriptor)


async def available_factors_session_builder(authentication_service, credentials_id):
	factors = []
	for factor in authentication_service.LoginFactors.values():
		if await factor.is_eligible({"credentials_id": credentials_id}):
			factors.append(factor.Type)
	return ((SessionAdapter.FN.Authentication.AvailableFactors, factors),)


def cookie_session_builder():
	# TODO: Shorten back to 32 bytes once unencrypted cookies are obsoleted
	token_length = 16 + 32  # The first part is AES CBC init vector, the second is the actual token
	yield (SessionAdapter.FN.Cookie.Id, secrets.token_bytes(token_length))
