"""Account dialog: sign up / sign in, and the state of cross-device sync.

Deliberately small: one email, one password, one button. The password is never stored —
only the refresh token Firebase returns, which "Esci" deletes.
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from .cloud import CloudClient, CloudError


class AccountDialog(QDialog):
    """Sign in or create an account, then sync history and favourites."""

    signed_in = Signal()
    signed_out = Signal()
    sync_requested = Signal()

    def __init__(self, client: CloudClient, parent=None) -> None:
        super().__init__(parent)
        self.client = client
        self.setWindowTitle("Account · Sincronizzazione")
        self.setModal(True)
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        intro = QLabel(
            "Accedi per ritrovare cronologia e preferiti su PC e telefono.\n"
            "Se non hai un account, scrivi email e password e crealo."
        )
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.email_input = QLineEdit()
        self.email_input.setPlaceholderText("La tua email")
        layout.addWidget(self.email_input)

        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("Password (almeno 6 caratteri)")
        self.password_input.setEchoMode(QLineEdit.Password)
        self.password_input.returnPressed.connect(self._sign_in)
        layout.addWidget(self.password_input)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.signup_button = QPushButton("Crea account")
        self.signup_button.setObjectName("Ghost")
        self.signup_button.clicked.connect(self._sign_up)
        buttons.addWidget(self.signup_button)

        self.signin_button = QPushButton("Accedi")
        self.signin_button.setObjectName("Primary")
        self.signin_button.clicked.connect(self._sign_in)
        buttons.addWidget(self.signin_button)

        self.signout_button = QPushButton("Esci")
        self.signout_button.setObjectName("Danger")
        self.signout_button.clicked.connect(self._sign_out)
        buttons.addWidget(self.signout_button)

        self.sync_button = QPushButton("Sincronizza ora")
        self.sync_button.setObjectName("Primary")
        self.sync_button.clicked.connect(self._sync_now)
        buttons.addWidget(self.sync_button)
        layout.addLayout(buttons)

        close = QPushButton("Chiudi")
        close.setObjectName("Ghost")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)

        self._refresh_state()

    # ------------------------------------------------------------------ #
    def _refresh_state(self) -> None:
        """Show either the sign-in form or the signed-in summary, never both."""
        config = self.client.config
        signed_in = config.signed_in
        for widget in (self.email_input, self.password_input):
            widget.setVisible(not signed_in)
        self.signup_button.setVisible(not signed_in)
        self.signin_button.setVisible(not signed_in)
        self.signout_button.setVisible(signed_in)
        self.sync_button.setVisible(signed_in)
        if signed_in:
            when = config.last_sync
            detail = (
                f"Ultima sincronizzazione: {time.strftime('%d/%m alle %H:%M', time.localtime(when))}"
                if when
                else "Non hai ancora sincronizzato."
            )
            self.status.setText(f"Collegato come {config.email}.\n{detail}")
        else:
            self.status.setText("")

    def _busy(self, message: str) -> None:
        self.status.setText(message)
        self.setEnabled(False)
        self.repaint()

    def _done(self) -> None:
        self.setEnabled(True)

    # ------------------------------------------------------------------ #
    def _credentials(self) -> tuple[str, str] | None:
        email = self.email_input.text().strip()
        password = self.password_input.text()
        if not email or "@" not in email:
            self.status.setText("Inserisci un indirizzo email valido.")
            return None
        if len(password) < 6:
            self.status.setText("La password deve avere almeno 6 caratteri.")
            return None
        return email, password

    def _sign_up(self) -> None:
        creds = self._credentials()
        if not creds:
            return
        self._busy("Creazione account…")
        try:
            self.client.sign_up(*creds)
        except CloudError as exc:
            self._done()
            self.status.setText(str(exc))
            return
        self._after_auth()

    def _sign_in(self) -> None:
        creds = self._credentials()
        if not creds:
            return
        self._busy("Accesso…")
        try:
            self.client.sign_in(*creds)
        except CloudError as exc:
            self._done()
            self.status.setText(str(exc))
            return
        self._after_auth()

    def _after_auth(self) -> None:
        self._done()
        self.password_input.clear()  # never keep the password around
        self._refresh_state()
        self.signed_in.emit()
        self.sync_requested.emit()  # first sync merges this device into the account

    def _sign_out(self) -> None:
        self.client.sign_out()
        self._refresh_state()
        self.signed_out.emit()

    def _sync_now(self) -> None:
        self.status.setText("Sincronizzazione in corso…")
        self.sync_requested.emit()

    def report(self, message: str) -> None:
        """Called back by the main window when a sync round finishes."""
        self.status.setText(message)
        self._refresh_state() if self.client.config.signed_in else None
