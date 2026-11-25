"""Create the first Platform Admin: ``python -m app.db.bootstrap_admin --email X``.

Also ``make admin EMAIL=X``. Promotes an existing, active, email-verified
account to PLATFORM_ADMIN. There is deliberately no HTTP route for this:
running it requires shell access to the server (or the API container), which
is what makes it safe to bootstrap the first admin. Register the account
through ``/auth/register`` and verify the email first; no password is ever
passed on the command line. Idempotent, and audited as a SYSTEM action.
"""

import argparse
import asyncio
import logging
import sys

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db import repositories as repo
from app.db.models import UserRole, UserStatus
from app.db.session import create_engine_and_session_factory
from app.services.auth import normalize_email

logger = logging.getLogger("app.bootstrap_admin")


class BootstrapError(Exception):
    pass


async def promote_to_platform_admin(db: AsyncSession, email: str) -> bool:
    """Promote the account; returns False if it already was an admin."""
    user = await repo.get_user_by_email(db, normalize_email(email))
    if user is None or user.status == UserStatus.DELETED:
        raise BootstrapError(f"No account with email {email!r}; register it first")
    if user.status != UserStatus.ACTIVE:
        raise BootstrapError("The account is suspended; it cannot become an admin")
    if user.email_verified_at is None:
        raise BootstrapError("Verify the account's email before promoting it")
    if user.global_role == UserRole.PLATFORM_ADMIN:
        return False
    previous = user.global_role
    user.global_role = UserRole.PLATFORM_ADMIN
    await repo.add_audit_entry(
        db,
        actor_user_id=None,
        actor_type="SYSTEM",
        entity_type="user",
        entity_id=user.id,
        action="admin.bootstrap_promoted",
        before_data={"global_role": previous.value},
        after_data={"global_role": UserRole.PLATFORM_ADMIN.value},
        metadata={"source": "app.db.bootstrap_admin"},
    )
    await db.flush()
    return True


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", required=True, help="email of an existing verified account")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging("INFO")
    engine, factory = create_engine_and_session_factory(settings.database_url)
    try:
        async with factory() as db:
            try:
                changed = await promote_to_platform_admin(db, args.email)
            except BootstrapError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            await db.commit()
    finally:
        await engine.dispose()
    print("promoted to PLATFORM_ADMIN" if changed else "already a PLATFORM_ADMIN")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
