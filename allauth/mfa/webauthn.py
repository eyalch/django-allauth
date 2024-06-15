import base64
import os
from typing import Any, Dict, List, Optional

from django.contrib.auth import get_user_model

import fido2.features
from fido2.server import Fido2Server
from fido2.utils import websafe_decode, websafe_encode
from fido2.webauthn import (
    AttestedCredentialData,
    AuthenticationResponse,
    AuthenticatorData,
    PublicKeyCredentialRpEntity,
    PublicKeyCredentialUserEntity,
    RegistrationResponse,
    UserVerificationRequirement,
)

from allauth.account.utils import url_str_to_user_pk
from allauth.core import context
from allauth.mfa import app_settings
from allauth.mfa.adapter import get_adapter
from allauth.mfa.models import Authenticator


if not fido2.features.webauthn_json_mapping.enabled:
    fido2.features.webauthn_json_mapping.enabled = True


CHALLENGE_SESSION_KEY = "mfa.webauthn.challenge"
STATE_SESSION_KEY = "mfa.webauthn.state"


def build_user_payload(user) -> PublicKeyCredentialUserEntity:
    kwargs = get_adapter().get_public_key_credential_user_entity(user)
    return PublicKeyCredentialUserEntity(**kwargs)


def generate_challenge() -> bytes:
    challenge = context.request.session.get(CHALLENGE_SESSION_KEY)
    if challenge is not None:
        return websafe_decode(challenge)
    challenge = os.urandom(32)
    context.request.session[CHALLENGE_SESSION_KEY] = websafe_encode(challenge)
    return challenge


def consume_challenge() -> None:
    context.request.session.pop(CHALLENGE_SESSION_KEY, None)


def get_state() -> Optional[Dict]:
    return context.request.session.get(STATE_SESSION_KEY)


def set_state(state: Dict) -> None:
    context.request.session[STATE_SESSION_KEY] = state


def clear_state() -> None:
    context.request.session.pop(STATE_SESSION_KEY, None)


def get_server() -> Fido2Server:
    rp_id = "localhost"  # FIXME
    rp = PublicKeyCredentialRpEntity(name="allauth", id=rp_id)
    verify_origin = None
    if app_settings.WEBAUTHN_ALLOW_INSECURE_ORIGIN:
        verify_origin = lambda o: True  # noqa
    server = Fido2Server(rp, verify_origin=verify_origin)
    return server


def parse_registration_response(response: Any) -> RegistrationResponse:
    try:
        return RegistrationResponse.from_dict(response)
    except TypeError:
        raise get_adapter().validation_error("incorrect_code")


def begin_registration(user) -> Dict:
    server = get_server()
    credentials = get_credentials(user)
    registration_data, state = server.register_begin(
        user=build_user_payload(user),
        credentials=credentials,
        user_verification=UserVerificationRequirement.DISCOURAGED,
        challenge=generate_challenge(),
    )
    set_state(state)
    return dict(registration_data)


def serialize_authenticator_data(authenticator_data: AuthenticatorData) -> str:
    return base64.b64encode(bytes(authenticator_data)).decode("ascii")


def complete_registration(credential: Dict) -> AuthenticatorData:
    server = get_server()
    state = get_state()
    # FIXME: handle invalid/absent state
    binding = server.register_complete(state, credential)
    consume_challenge()
    clear_state()
    return binding


def get_credentials(user) -> list[AttestedCredentialData]:
    credentials: List[AttestedCredentialData] = []
    authenticators = Authenticator.objects.filter(
        user=user, type=Authenticator.Type.WEBAUTHN
    )
    for authenticator in authenticators:
        credential_data = authenticator.wrap().authenticator_data.credential_data
        if credential_data:
            credentials.append(authenticator.wrap().authenticator_data.credential_data)
    return credentials


# FIXME: Why user?
def get_authenticator_by_credential_id(
    user, credential_id: bytes
) -> Optional[Authenticator]:
    authenticators = Authenticator.objects.filter(type=Authenticator.Type.WEBAUTHN)
    for authenticator in authenticators:
        if (
            credential_id
            == authenticator.wrap().authenticator_data.credential_data.credential_id
        ):
            return authenticator
    return None


def parse_authentication_response(response: Any) -> AuthenticationResponse:
    try:
        return AuthenticationResponse.from_dict(response)
    except TypeError:
        raise get_adapter().validation_error("incorrect_code")


def begin_authentication(user=None) -> Dict:
    server = get_server()
    request_options, state = server.authenticate_begin(
        credentials=get_credentials(user) if user else [],
        user_verification=UserVerificationRequirement.PREFERRED,
        challenge=generate_challenge(),
    )
    set_state(state)
    return dict(request_options)


def extract_user_from_response(response: Dict):
    try:
        user_handle = response.get("response", {}).get("userHandle")
        user_pk = url_str_to_user_pk(websafe_decode(user_handle).decode("utf8"))
    except (ValueError, TypeError, KeyError):
        raise get_adapter().validation_error("incorrect_code")
    user = get_user_model().objects.filter(pk=user_pk).first()
    if not user:
        raise get_adapter().validation_error("incorrect_code")
    return user


def complete_authentication(user, response: Dict):
    if user is None:
        user = extract_user_from_response(response)
    credentials = get_credentials(user)
    server = get_server()
    state = get_state()
    # FIXME: handle invalid/absent state
    try:
        binding = server.authenticate_complete(state, credentials, response)
    except ValueError as e:
        # ValueError: Unknown credential ID.
        raise get_adapter().validation_error("incorrect_code") from e
    consume_challenge()
    clear_state()
    return get_authenticator_by_credential_id(user, binding.credential_id)


class WebAuthn:
    def __init__(self, instance):
        self.instance = instance

    @classmethod
    def add(
        cls, user, name: str, authenticator_data: str, passwordless: bool
    ) -> "WebAuthn":
        instance = Authenticator(
            user=user,
            type=Authenticator.Type.WEBAUTHN,
            data={
                "name": name,
                "authenticator_data": authenticator_data,
                "passwordless": passwordless,
            },
        )
        instance.save()
        return cls(instance)

    @property
    def name(self) -> str:
        return self.instance.data["name"]

    @property
    def authenticator_data(self) -> AuthenticatorData:
        return AuthenticatorData(
            base64.b64decode(self.instance.data["authenticator_data"])
        )

    @property
    def is_passwordless(self) -> bool:
        """Whether or not a key is resident is not stored in the authenticator
        data. There is the `credProps` extension that is supposed to offer this
        info, but that extension is not guaranteed to be working either. So, we
        just store whether or not the key is passwordless at creation time. Do
        note that this cannot be fully trusted as it is posted by the frontend
        and stored as is.
        """
        return self.instance.data["passwordless"]
