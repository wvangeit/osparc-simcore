import sys
from datetime import UTC, datetime

import typer
from servicelib.utils_secrets import generate_password
from simcore_postgres_database.models.confirmations import ConfirmationAction
from yarl import URL

from ._invitations_service import ConfirmedInvitationData, get_invitation_url


def invitations(
    base_url: str,
    issuer_email: str,
    trial_days: int | None = None,
    user_id: int = 1,
    num_codes: int = 15,
    code_length: int = 30,
) -> None:
    """Generates a list of invitation links for registration"""

    invitation = ConfirmedInvitationData(issuer=issuer_email, trial_account_days=trial_days)  # type: ignore[call-arg] # guest field is deprecated

    codes: list[str] = [generate_password(code_length) for _ in range(num_codes)]

    typer.secho(
        "{:-^100}".format("invitations.md"),
        fg=typer.colors.BLUE,
    )

    for i, code in enumerate(codes, start=1):
        url = get_invitation_url(
            {"code": code, "action": ConfirmationAction.INVITATION.name},
            origin=URL(base_url),
        )
        typer.secho(f"{i:2d}. {url}")

    #
    # NOTE: An obvious improvement would be to inject the invitations directly from here
    #       into the database but for that I would add an authentication first. Could
    #       use login auth and give access to only ADMINS
    #

    typer.secho(
        "{:-^100}".format("postgres.csv"),
        fg=typer.colors.BLUE,
    )

    utcnow = datetime.now(tz=UTC)
    today: datetime = utcnow.today()
    print("code,user_id,action,data,created_at", file=sys.stdout)  # noqa: T201
    for n, code in enumerate(codes, start=1):
        print(f'{code},{user_id},INVITATION,"{{', file=sys.stdout)  # noqa: T201
        print(  # noqa: T201
            f'""guest"": ""invitation-{today.year:04d}{today.month:02d}{today.day:02d}-{n}"" ,',
            file=sys.stdout,
        )
        print(f'""issuer"" : ""{invitation.issuer}"" ,', file=sys.stdout)  # noqa: T201
        print(  # noqa: T201
            f'""trial_account_days"" : ""{invitation.trial_account_days}""',
            file=sys.stdout,
        )
        print('}}",{}'.format(utcnow.isoformat(sep=" ")), file=sys.stdout)  # noqa: T201

    typer.secho(
        "-" * 100,
        fg=typer.colors.BLUE,
    )
