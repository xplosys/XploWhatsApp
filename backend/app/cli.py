"""Operational commands: python -m app.cli <command>."""

from __future__ import annotations

import asyncio
import getpass
from typing import Any

import typer
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security.crypto import generate_kek
from app.core.security.jwt import generate_private_key_pem
from app.core.security.passwords import hash_password, validate_password_strength

cli = typer.Typer(help="XploWhatsApp operations", no_args_is_help=True)

# Plan limits are data. Prices and paid plans are configured in Phase 7.
DEFAULT_PLANS = [
    {
        "code": "trial",
        "name": "Free trial",
        "description": "14-day trial with core features.",
        "trial_days": 14,
        "is_public": False,
        "sort_order": 0,
        "features": [
            "1 WhatsApp number",
            "AI replies from your own documents",
            "Shared inbox and lead capture",
        ],
        "limits": {
            "max_users": 3,
            "max_whatsapp_numbers": 1,
            "max_knowledge_documents": 20,
            "max_storage_mb": 200,
            "monthly_ai_messages": 500,
            # 1 = may use XploSys's own AI keys (if the server has them); 0 = own keys only.
            "platform_ai_keys": 1,
        },
    },
]


@cli.command("bootstrap-db")
def bootstrap_db() -> None:
    """Create roles, extensions and default privileges (needs DATABASE_SUPERUSER_URL)."""
    from app.db.bootstrap import bootstrap_database

    asyncio.run(bootstrap_database(get_settings()))
    typer.echo("Database bootstrap complete.")


async def _seed_plans() -> int:
    from app.db.session import admin_transaction
    from app.modules.billing.models import SubscriptionPlan

    created = 0
    async with admin_transaction() as session:
        for plan in DEFAULT_PLANS:
            exists = await session.scalar(
                select(SubscriptionPlan).where(SubscriptionPlan.code == plan["code"])
            )
            if exists is None:
                session.add(SubscriptionPlan(**plan))
                created += 1
            else:
                # New limit keys reach existing plans; values already set are kept.
                limits: dict[str, Any] = plan["limits"]  # type: ignore[assignment]
                missing = {k: v for k, v in limits.items() if k not in exists.limits}
                if missing:
                    exists.limits = {**exists.limits, **missing}
    return created


@cli.command("seed")
def seed() -> None:
    """Insert default subscription plans if missing (idempotent)."""
    typer.echo(f"Plans created: {asyncio.run(_seed_plans())}")


async def _create_super_admin(email: str, full_name: str, password: str) -> None:
    from datetime import UTC, datetime

    from app.db.session import admin_transaction
    from app.modules.auth.models import User

    async with admin_transaction() as session:
        user = await session.scalar(select(User).where(User.email == email))
        if user is None:
            session.add(
                User(
                    email=email,
                    full_name=full_name,
                    password_hash=hash_password(password),
                    is_super_admin=True,
                    email_verified_at=datetime.now(UTC),
                )
            )
        else:
            user.is_super_admin = True


@cli.command("create-super-admin")
def create_super_admin(email: str, full_name: str = "Platform Admin") -> None:
    """Create (or promote) a Super Admin. They must enable MFA before using the admin console."""
    password = getpass.getpass("Password: ")
    problems = validate_password_strength(password, email)
    if problems:
        raise typer.BadParameter(" ".join(problems))
    if password != getpass.getpass("Repeat password: "):
        raise typer.BadParameter("Passwords do not match.")
    asyncio.run(_create_super_admin(email, full_name, password))
    typer.echo(f"Super Admin ready: {email}. Sign in and enable two-factor authentication.")


@cli.command("ensure-bucket")
def ensure_bucket_command() -> None:
    """Create the private knowledge bucket if it doesn't exist (needs S3_* settings)."""
    from app.modules.storage import s3

    if not s3.storage_configured():
        typer.echo("File storage is not configured (S3_*); skipping.")
        return
    created = asyncio.run(s3.ensure_bucket())
    typer.echo(f"Bucket {get_settings().s3_bucket} {'created' if created else 'already exists'}.")


@cli.command("generate-secrets")
def generate_secrets() -> None:
    """Print fresh values for JWT_PRIVATE_KEY_PEM and ENCRYPTION_KEYS (for .env)."""
    pem = generate_private_key_pem().strip().replace("\n", "\\n")
    typer.echo(f'JWT_PRIVATE_KEY_PEM="{pem}"')
    typer.echo(f"ENCRYPTION_KEYS=v1:{generate_kek()}")
    typer.echo("ENCRYPTION_ACTIVE_KEY_VERSION=v1")


if __name__ == "__main__":
    cli()
