from core.registration import (
    ChallengeRequest,
    ChallengeResponse,
    HumanVerificationRequired,
    RegistrationArtifacts,
)


def test_registration_artifacts_keeps_challenge_callback_optional():
    artifacts = RegistrationArtifacts()

    assert artifacts.challenge_callback is None


def test_challenge_request_uses_safe_defaults():
    request = ChallengeRequest(kind="two_factor", message="Complete 2FA")

    assert request.kind == "two_factor"
    assert request.message == "Complete 2FA"
    assert request.url == ""
    assert request.metadata == {}


def test_challenge_request_metadata_is_not_shared_between_instances():
    first = ChallengeRequest(kind="security_check")
    second = ChallengeRequest(kind="security_check")

    first.metadata["provider"] = "example"

    assert second.metadata == {}


def test_challenge_response_defaults_to_no_value():
    response = ChallengeResponse(completed=True)

    assert response.completed is True
    assert response.value == ""


def test_human_verification_required_carries_request():
    request = ChallengeRequest(kind="qr_confirmation", message="Scan QR code")
    error = HumanVerificationRequired(request)

    assert error.request is request
    assert str(error) == "Scan QR code"


def test_human_verification_required_falls_back_to_kind_for_message():
    request = ChallengeRequest(kind="security_check")
    error = HumanVerificationRequired(request)

    assert str(error) == "security_check"
