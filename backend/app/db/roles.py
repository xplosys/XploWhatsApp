"""Database role names. Passwords come from the connection URLs in settings."""

OWNER_ROLE = "xplo_owner"  # owns the schema, runs migrations
APP_ROLE = "xplo_app"  # API and workers; RLS always applies
ADMIN_ROLE = "xplo_admin"  # Super Admin module only; BYPASSRLS
DEFINER_ROLE = "xplo_definer"  # NOLOGIN, BYPASSRLS; owns narrow SECURITY DEFINER lookup functions
