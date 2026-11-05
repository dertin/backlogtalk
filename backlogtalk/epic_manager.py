"""Dialog for selecting and naming locally preferred Jira epics."""

import queue
import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from backlogtalk import core
from backlogtalk.qt_widgets import button, label


class EpicManager(QDialog):
    def __init__(self, parent, project, site, favorites, names, save_selection):
        super().__init__(parent)
        self.project = project
        self.site = site
        self.aliases = dict(names)
        self.save_selection = save_selection
        self.results = queue.Queue()
        self.setObjectName("instructionsDialog")
        self.setWindowTitle(f"Épicas / {project}")
        self.resize(740, 560)
        layout = QVBoxLayout(self)
        layout.addWidget(label(f"Épicas de {project}", "sectionTitle"))
        layout.addWidget(
            label(
                "Marcá las que querés ver en el selector. El nombre es local y no modifica Jira.",
                "muted",
            )
        )
        controls = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filtrar por clave o nombre…")
        controls.addWidget(self.search, 1)
        self.fetch = button("Buscar en Jira", self.fetch_epics)
        self.fetch.setObjectName("fetchEpics")
        controls.addWidget(self.fetch)
        layout.addLayout(controls)
        self.status = label(
            "Mostrando tus épicas guardadas. Buscá en Jira para ver todas.", "muted"
        )
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 2)
        self.table.setObjectName("epicsTable")
        self.table.setHorizontalHeaderLabels(["Mostrar / clave", "Nombre local (editable)"])
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table, 1)
        self.search.textChanged.connect(self.filter_rows)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(button("Cancelar", self.reject))
        row.addWidget(button("Guardar selección", self.save, "primary"))
        layout.addLayout(row)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll_results)
        self.finished.connect(self.timer.stop)
        self.timer.start(100)
        for key in favorites:
            self.add_epic(key, names.get(key, ""), True)

    def add_epic(self, key, name, checked):
        row = self.table.rowCount()
        self.table.insertRow(row)
        item = QTableWidgetItem(key)
        item.setFlags(
            (item.flags() | Qt.ItemFlag.ItemIsUserCheckable) & ~Qt.ItemFlag.ItemIsEditable
        )
        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.table.setItem(row, 0, item)
        self.table.setItem(row, 1, QTableWidgetItem(name))

    def filter_rows(self, *_):
        query = self.search.text().strip().casefold()
        for row in range(self.table.rowCount()):
            text = f"{self.table.item(row, 0).text()} {self.table.item(row, 1).text()}".casefold()
            self.table.setRowHidden(row, query not in text)

    def fetch_epics(self):
        self.fetch.setEnabled(False)
        self.status.setText("Consultando Jira…")

        results, project, site = self.results, self.project, self.site

        def worker():
            try:
                results.put((True, core.jira_epics(project, site)))
            except Exception as error:
                results.put((False, str(error)))

        threading.Thread(target=worker, daemon=True, name="backlogtalk-jira-epics").start()

    def poll_results(self):
        try:
            ok, result = self.results.get_nowait()
        except queue.Empty:
            return
        self.fetch.setEnabled(True)
        if not ok:
            self.status.setText(f"No se pudieron cargar las épicas: {result}")
            return
        existing = {self.table.item(row, 0).text(): row for row in range(self.table.rowCount())}
        for epic in result:
            row = existing.get(epic["key"])
            if row is None:
                self.add_epic(
                    epic["key"],
                    self.aliases.get(epic["key"], epic["summary"]),
                    False,
                )
            elif not self.table.item(row, 1).text().strip():
                self.table.item(row, 1).setText(epic["summary"])
        self.filter_rows()
        self.status.setText(f"{len(result)} épicas encontradas en Jira. Marcá las que usás.")

    def save(self):
        selected = []
        names = dict(self.aliases)
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            key = item.text()
            if item.checkState() == Qt.CheckState.Checked:
                selected.append(key)
                name = self.table.item(row, 1).text().strip()
                if name:
                    names[key] = name
                else:
                    names.pop(key, None)
        try:
            self.save_selection(selected, names)
        except Exception as error:
            self.status.setText(f"No se pudo guardar la selección: {error}")
            return
        self.accept()
