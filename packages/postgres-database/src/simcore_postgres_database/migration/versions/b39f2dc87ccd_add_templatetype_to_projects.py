"""add templateType to projects

Revision ID: b39f2dc87ccd
Revises: fc1701bb7e93
Create Date: 2025-05-14 11:59:27.033449+00:00

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b39f2dc87ccd"
down_revision = "fc1701bb7e93"
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    # Create enum type first
    project_template_type = sa.Enum(
        "TEMPLATE", "TUTORIAL", "HYPERTOOL", name="projecttemplatetype"
    )
    project_template_type.create(op.get_bind())

    op.add_column(
        "projects",
        sa.Column(
            "template_type",
            project_template_type,
            nullable=True,
            default=None,
        ),
    )
    # ### end Alembic commands ###
    op.execute("UPDATE projects SET template_type='TEMPLATE' WHERE type='TEMPLATE'")


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column("projects", "template_type")
    # ### end Alembic commands ###
    sa.Enum(name="projecttemplatetype").drop(op.get_bind())
