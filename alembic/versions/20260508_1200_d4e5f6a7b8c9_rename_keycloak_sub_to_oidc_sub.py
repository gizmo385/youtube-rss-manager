"""rename keycloak_sub to oidc_sub

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-05-08 12:00:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op


revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('users', 'keycloak_sub', new_column_name='oidc_sub')
    op.drop_index('ix_users_keycloak_sub', table_name='users')
    op.create_index(op.f('ix_users_oidc_sub'), 'users', ['oidc_sub'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_users_oidc_sub'), table_name='users')
    op.create_index('ix_users_keycloak_sub', 'users', ['keycloak_sub'], unique=True)
    op.alter_column('users', 'oidc_sub', new_column_name='keycloak_sub')
