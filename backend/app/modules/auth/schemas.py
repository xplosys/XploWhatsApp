from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints

from app.modules.tenants.models import TenantRole

Password = Annotated[str, Field(min_length=1, max_length=128)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Token = Annotated[str, StringConstraints(strip_whitespace=True, min_length=16, max_length=256)]
OtpCode = Annotated[str, StringConstraints(strip_whitespace=True, min_length=6, max_length=32)]


class RegisterRequest(BaseModel):
    email: EmailStr
    password: Password
    full_name: Name
    business_name: Name | None = None
    country: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")] = "IN"
    timezone: Annotated[str, StringConstraints(max_length=64)] = "Asia/Kolkata"
    invitation_token: Token | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: Password


class MfaVerifyRequest(BaseModel):
    mfa_token: Annotated[str, StringConstraints(min_length=16, max_length=2048)]
    code: OtpCode


class SwitchTenantRequest(BaseModel):
    tenant_id: uuid.UUID


class TokenRequest(BaseModel):
    token: Token


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: Token
    password: Password


class ChangePasswordRequest(BaseModel):
    current_password: Password
    new_password: Password


class MfaEnableRequest(BaseModel):
    code: OtpCode


class MfaDisableRequest(BaseModel):
    password: Password
    code: OtpCode


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str
    email_verified: bool
    mfa_enabled: bool
    is_super_admin: bool
    created_at: datetime


class MembershipSummary(BaseModel):
    tenant_id: uuid.UUID
    tenant_name: str
    tenant_slug: str
    role: TenantRole
    status: str


class SessionOut(BaseModel):
    """Returned by login/refresh/switch. The refresh token is only ever sent as an HttpOnly cookie."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut
    active_tenant_id: uuid.UUID | None
    memberships: list[MembershipSummary]


class MfaChallengeOut(BaseModel):
    mfa_required: bool = True
    mfa_token: str


class MeOut(BaseModel):
    user: UserOut
    active_tenant_id: uuid.UUID | None
    role: TenantRole | None
    permissions: list[str]
    memberships: list[MembershipSummary]
    mfa_verified: bool


class MfaSetupOut(BaseModel):
    secret: str
    otpauth_uri: str


class RecoveryCodesOut(BaseModel):
    recovery_codes: list[str]


class InvitationPreviewOut(BaseModel):
    tenant_name: str
    email: str
    role: TenantRole
    expires_at: datetime


class MessageOut(BaseModel):
    message: str
