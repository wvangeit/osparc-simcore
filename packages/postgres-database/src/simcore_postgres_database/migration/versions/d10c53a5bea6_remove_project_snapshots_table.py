"""remove project snapshots table

Revision ID: d10c53a5bea6
Revises: 389bf931b51a
Create Date: 2021-09-03 09:54:57.910782+00:00

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "d10c53a5bea6"
down_revision = "389bf931b51a"
branch_labels = None
depends_on = None


def upgrade():
    #
    # NOTE: This table was never released to production therefore there is no need
    # to migrate any data, just drop the table down
    #
    op.drop_table("projects_snapshots")


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "projects_snapshots",
        sa.Column("id", sa.BIGINT(), autoincrement=True, nullable=False),
        sa.Column("name", sa.VARCHAR(), autoincrement=False, nullable=False),
        sa.Column(
            "created_at", postgresql.TIMESTAMP(), autoincrement=False, nullable=False
        ),
        sa.Column("parent_uuid", sa.VARCHAR(), autoincrement=False, nullable=False),
        sa.Column("project_uuid", sa.VARCHAR(), autoincrement=False, nullable=False),
        sa.ForeignKeyConstraint(
            ["parent_uuid"],
            ["projects.uuid"],
            name="fk_snapshots_parent_uuid_projects",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_uuid"],
            ["projects.uuid"],
            name="fk_snapshots_project_uuid_projects",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="projects_snapshots_pkey"),
        sa.UniqueConstraint(
            "parent_uuid", "created_at", name="snapshot_from_project_uniqueness"
        ),
        sa.UniqueConstraint("project_uuid", name="projects_snapshots_project_uuid_key"),
    )
    # ### end Alembic commands ###