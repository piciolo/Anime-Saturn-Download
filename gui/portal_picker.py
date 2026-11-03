"""La domanda «da dove guardi?», all'apertura dell'app.

Gemella di ``lib/ui/portal_picker.dart`` dell'app Android: stesse schede, stessi testi,
stessa spunta «ricorda questa scelta». Compare finché non si spunta «ricorda»; da lì in
poi l'app parte diretta sul catalogo, e la domanda torna dal pulsante del portale
nell'intestazione.

La scelta vale per catalogo, ricerca, suggerimenti, preferiti e cronologia: l'elenco
mostra un portale alla volta. Filtra la vista, non l'archivio — nessuna voce viene
spostata o cancellata, e torna in elenco appena si torna sul suo portale. Aprirne una la
riproduce comunque dal portale da cui viene (:mod:`gui.sources.registry`): un episodio
vive dove è stato trovato, e una voce che si apre sul sito sbagliato non si apre affatto.

DUE DIFFERENZE DALL'ANDROID, volute.

La prima: qui c'è anche «mostrali tutti e due». Su Android la schermata non si può
chiudere, quindi un portale va scelto; su un computer quella porta chiusa davanti a chi
ha solo aperto l'app per guardare qualcosa sarebbe sgradevole, e soprattutto *mostrarli
tutti è ciò che questa app ha sempre fatto*: un selettore che lo rendesse impossibile
sarebbe un peggioramento, non una funzione in più. Lo stato esiste già in entrambe le app
(è il ``null`` che precede la scelta, e il ripiego quando l'identificativo salvato non si
riconosce): qui gli si dà solo un nome e un pulsante.

La seconda: chiudere la finestra senza scegliere non insiste. Vale «per adesso tutti»,
cioè lo stato di prima, e la domanda ritorna al prossimo avvio.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

#: Scelta «tutti i portali», da distinguere da «non ho ancora scelto».
#:
#: Non è l'identificativo di nessun portale, di proposito: così un confronto con
#: ``source.id`` non può sbagliarsi, e l'impostazione vuota resta libera di significare
#: l'unica altra cosa che deve significare, cioè «chiedimelo».
ALL_PORTALS = "*"

# Nome e riga di presentazione dei portali, come sull'Android.
_PORTALS = (
    ("animesaturn", "🌐", "AnimeSaturn", "Catalogo ampio, episodi anche in italiano."),
    ("animeunity", "🧭", "AnimeUnity", "Spesso ha quello che manca sull'altro."),
)


def portal_label(portal: str | None) -> str:
    """Il nome da mostrare per un portale, per l'intestazione e gli avvisi."""
    for portal_id, _icon, name, _description in _PORTALS:
        if portal_id == portal:
            return name
    return "Tutti i portali"


class PortalPicker(QDialog):
    """Chiede da quale portale guardare. Lascia la scelta in :attr:`portal`."""

    def __init__(self, *, remember: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Da dove guardi?")
        self.setModal(True)
        self.setMinimumWidth(520)

        #: L'identificativo scelto, :data:`ALL_PORTALS`, o ``""`` se la finestra è stata
        #: chiusa senza scegliere.
        self.portal: str = ""
        #: Se la scelta va ricordata per i prossimi avvii.
        self.remember: bool = remember

        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 22, 26, 20)
        layout.setSpacing(14)

        title = QLabel("Da dove guardi?")
        title.setObjectName("SectionTitle")
        layout.addWidget(title)

        intro = QLabel(
            "Catalogo, ricerca, preferiti e «continua a guardare» mostreranno solo il "
            "portale scelto. Niente viene perso: cambiando portale torna in elenco "
            "quello che sta là."
        )
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        for portal_id, icon, name, description in _PORTALS:
            layout.addWidget(self._card(portal_id, icon, name, description))

        both = QPushButton("Mostrali tutti e due")
        both.setObjectName("Ghost")
        both.setToolTip(
            "Come ha sempre fatto questa app: i risultati dei due portali insieme, "
            "senza doppioni."
        )
        both.clicked.connect(lambda: self._choose(ALL_PORTALS))
        layout.addWidget(both)

        self._remember_box = QCheckBox("Ricorda questa scelta")
        self._remember_box.setChecked(remember)
        layout.addWidget(self._remember_box)

        note = QLabel(
            "Non chiedere più all'avvio. Si cambia quando vuoi dal pulsante del "
            "portale, in alto."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        layout.addWidget(note)

    # ------------------------------------------------------------------ #
    def _card(self, portal_id: str, icon: str, name: str, description: str) -> QFrame:
        row = QFrame()
        row.setObjectName("Row")
        outer = QHBoxLayout(row)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(14)

        badge = QLabel(icon)
        badge.setAlignment(Qt.AlignCenter)
        badge.setFixedWidth(28)
        outer.addWidget(badge)

        text = QVBoxLayout()
        text.setSpacing(2)
        heading = QLabel(name)
        heading.setStyleSheet("font-weight:600;font-size:15px;")
        text.addWidget(heading)
        subtitle = QLabel(description)
        subtitle.setObjectName("Muted")
        subtitle.setWordWrap(True)
        text.addWidget(subtitle)
        outer.addLayout(text, 1)

        # Il pulsante, e non solo la riga cliccabile: in una finestra si arriva anche con
        # Tab e si conferma con Invio, e una riga che reagisce al solo clic lascerebbe
        # fuori chi non usa il mouse.
        choose = QPushButton("Scegli")
        choose.setObjectName("Primary")
        choose.clicked.connect(lambda _=False, p=portal_id: self._choose(p))
        outer.addWidget(choose)
        return row

    def _choose(self, portal: str) -> None:
        self.portal = portal
        self.remember = self._remember_box.isChecked()
        self.accept()


def choose_portal(settings, *, current: str | None = None, parent=None) -> str | None:
    """Chiede il portale e restituisce quello da usare adesso (``None`` = tutti).

    Se l'utente spunta «ricorda», la scelta finisce nelle impostazioni; se la toglie, il
    predefinito salvato viene rimosso e la domanda tornerà al prossimo avvio.

    Chiudere senza scegliere restituisce ``current`` e non salva niente: riaprire la
    domanda per curiosità e chiuderla non deve cambiare il portale su cui si stava
    guardando. All'avvio ``current`` è ``None``, cioè tutti, che è esattamente come
    l'app si è comportata finora.
    """
    dialog = PortalPicker(remember=bool(settings.default_portal), parent=parent)
    dialog.exec()
    if not dialog.portal:
        return current
    settings.default_portal = dialog.portal if dialog.remember else ""
    return None if dialog.portal == ALL_PORTALS else dialog.portal


def stored_portal(settings) -> str | None:
    """Il portale salvato, tradotto in ciò che il registro e gli elenchi si aspettano.

    ``None`` vale «tutti», sia quando è la scelta salvata sia quando non c'è ancora
    nessuna scelta: è lo stesso comportamento, e distinguerli serve solo a decidere se
    fare la domanda all'avvio (lo fa :func:`should_ask`).
    """
    saved = settings.default_portal
    return None if saved in ("", ALL_PORTALS) else saved


def should_ask(settings) -> bool:
    """Se la domanda va fatta all'avvio: solo quando non c'è nessuna scelta salvata."""
    return not settings.default_portal
