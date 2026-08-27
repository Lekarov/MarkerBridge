"""
MarkerBridge — interface graphique (thème noir & violet) pour convertir les
chapitres OBS en marqueurs Premiere. Permet de récupérer les vidéos depuis le
PC de stream (partage réseau), de choisir un dossier et/ou des fichiers .mp4,
ainsi qu'un dossier d'export pour les XML générés.

S'appuie entièrement sur la logique déjà écrite dans markerbridge.py.
"""

import ctypes
import datetime
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk

import markerbridge as core

# Fichier où sont mémorisés les chemins réseau saisis (source stream / destination),
# pour ne pas avoir à les ressaisir à chaque lancement. Stocké dans le dossier de
# données applicatif (%APPDATA%/DoktorP3st/MarkerBridge), pas à côté du script :
# fonctionne quel que soit l'endroit où le programme est installé, et ne mélange
# jamais les chemins réseau personnels de l'utilisateur avec le code du projet.
CHEMIN_CONFIG = os.path.join(core.DOSSIER_DONNEES, "config_gui.json")

# Nom du mutex Windows utilisé pour empêcher plusieurs instances de la GUI de
# tourner en même temps (évite deux récupérations/conversions concurrentes sur
# les mêmes dossiers). "Global\\" le rend visible pour toutes les sessions.
NOM_MUTEX_INSTANCE_UNIQUE = "Global\\MarkerBridge_InstanceUnique"
ERREUR_DEJA_EXISTANT = 183  # code Windows ERROR_ALREADY_EXISTS


def _acquerir_verrou_instance_unique():
    """Tente de prendre un mutex nommé au niveau du système.

    Retourne le handle du mutex si cette instance est la seule ouverte, ou
    None si une autre instance de la GUI tourne déjà. Le handle doit être
    conservé en vie (variable globale/attribut) tant que l'app tourne : le
    verrou est relâché automatiquement quand le processus se termine.
    """
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, NOM_MUTEX_INSTANCE_UNIQUE)
    if ctypes.windll.kernel32.GetLastError() == ERREUR_DEJA_EXISTANT:
        return None
    return mutex

# Palette "noir & violet" — thème sombre épuré, un seul accent (violet) pour
# hiérarchiser l'attention (actions principales, focus, progression).
COULEUR_FOND = "#0c0c11"
COULEUR_FOND_PANNEAU = "#16151d"
COULEUR_FOND_CHAMP = "#1e1d27"
COULEUR_TEXTE = "#eae9f2"
COULEUR_TEXTE_ATTENUE = "#8d8a9c"
COULEUR_ACCENT = "#8b5cf6"
COULEUR_ACCENT_HOVER = "#a78bfa"
COULEUR_ACCENT_ACTIF = "#7c3aed"
COULEUR_BORDURE = "#28262f"
COULEUR_DANGER = "#f87171"
COULEUR_DANGER_HOVER = "#fca5a5"

# Modèles de renommage proposés dans les Paramètres (l'utilisateur peut aussi
# taper le sien) : {n} = numéro auto-incrémenté, {date} = AAAA-MM-JJ du jour de
# la récupération, {heure} = HH-MM-SS. "Partie {n}" reste la valeur par défaut
# (usage personnel d'origine), les autres modèles servent aux personnes qui
# récupèrent l'outil et veulent une autre convention de nommage.
MODELES_RENOMMAGE_PREDEFINIS = [
    "Partie {n}",
    "{date} - Partie {n}",
    "Enregistrement {n}",
    "Session {date}_{heure}",
    "{date}_{heure} - Partie {n}",
]
MODELE_RENOMMAGE_PAR_DEFAUT = MODELES_RENOMMAGE_PREDEFINIS[0]


class ApplicationMarqueurs(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title(f"MarkerBridge  v{core.VERSION}")
        self.geometry("800x860")
        self.minsize(680, 700)
        self.configure(bg=COULEUR_FOND)

        self.fichiers_mp4 = []  # liste des chemins complets sélectionnés
        self.dossier_export = None  # None = exporter à côté des sources
        self.file_attente = queue.Queue()
        self.conversion_en_cours = False
        self.recuperation_en_cours = False
        self.purge_en_cours = False

        self.config = self._charger_config()

        self._configurer_style()
        self._construire_interface()
        self.fenetre_parametres = self._construire_fenetre_parametres()
        self.protocol("WM_DELETE_WINDOW", self._fermer_application)
        self.after(100, self._traiter_file_attente)

    # -------------------------------------------------------- config persistée

    def _charger_config(self):
        """Recharge les chemins réseau saisis lors d'un précédent lancement."""
        try:
            with open(CHEMIN_CONFIG, "r", encoding="utf-8") as fichier:
                return json.load(fichier)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _sauvegarder_config(self):
        # Décalage persisté en nombre (pas en texte brut) : une saisie invalide
        # ne doit pas se retrouver figée dans la config.
        try:
            decalage = float(self.var_decalage.get().strip().replace(",", "."))
        except ValueError:
            decalage = core.DECALAGE_PAR_DEFAUT_SEC

        donnees = {
            "dossier_source_stream": self.var_dossier_source_stream.get(),
            "dossier_reception": self.var_dossier_reception.get(),
            "deplacer_apres_copie": self.var_deplacer_apres_copie.get(),
            "decalage_sec": decalage,
            "renommage_actif": self.var_renommage_actif.get(),
            "modele_renommage": self.var_modele_renommage.get(),
        }
        try:
            with open(CHEMIN_CONFIG, "w", encoding="utf-8") as fichier:
                json.dump(donnees, fichier, ensure_ascii=False, indent=2)
        except OSError:
            pass  # la persistance de config n'est pas critique

    def _fermer_application(self):
        self._sauvegarder_config()
        self.destroy()

    # ------------------------------------------------------------------ UI

    def _configurer_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        POLICE = "Segoe UI"

        # --- Conteneurs : deux variantes, fond principal (noir) et fond
        # "carte" (légèrement plus clair), pour construire des sections
        # visuellement séparées façon panneaux/cartes.
        style.configure("TFrame", background=COULEUR_FOND)
        style.configure("Panneau.TFrame", background=COULEUR_FOND_PANNEAU)

        # --- Texte, décliné pour chaque fond (principal vs carte).
        style.configure(
            "TLabel", background=COULEUR_FOND, foreground=COULEUR_TEXTE, font=(POLICE, 10)
        )
        style.configure(
            "Panneau.TLabel",
            background=COULEUR_FOND_PANNEAU,
            foreground=COULEUR_TEXTE,
            font=(POLICE, 10),
        )
        style.configure(
            "Titre.TLabel",
            background=COULEUR_FOND,
            foreground=COULEUR_TEXTE,
            font=(POLICE, 15, "bold"),
        )
        style.configure(
            "Attenue.TLabel",
            background=COULEUR_FOND,
            foreground=COULEUR_TEXTE_ATTENUE,
            font=(POLICE, 9),
        )
        style.configure(
            "PanneauAttenue.TLabel",
            background=COULEUR_FOND_PANNEAU,
            foreground=COULEUR_TEXTE_ATTENUE,
            font=(POLICE, 9),
        )
        # En-tête de carte : petite majuscule accentuée en violet, façon
        # "eyebrow" des interfaces pro modernes.
        style.configure(
            "TitreCarte.TLabel",
            background=COULEUR_FOND_PANNEAU,
            foreground=COULEUR_ACCENT_HOVER,
            font=(POLICE, 9, "bold"),
        )
        style.configure(
            "Lien.TLabel",
            background=COULEUR_FOND_PANNEAU,
            foreground=COULEUR_ACCENT_HOVER,
            font=(POLICE, 9, "underline"),
        )

        # --- Boutons secondaires (neutres) et principal (violet plein).
        style.configure(
            "TButton",
            background=COULEUR_FOND_CHAMP,
            foreground=COULEUR_TEXTE,
            borderwidth=0,
            focuscolor=COULEUR_FOND_CHAMP,
            padding=(12, 7),
            font=(POLICE, 9),
        )
        style.map(
            "TButton",
            background=[("active", COULEUR_BORDURE), ("disabled", COULEUR_FOND_PANNEAU)],
            foreground=[("disabled", COULEUR_TEXTE_ATTENUE)],
        )

        style.configure(
            "Accent.TButton",
            background=COULEUR_ACCENT,
            foreground="#ffffff",
            borderwidth=0,
            focuscolor=COULEUR_ACCENT,
            padding=(16, 9),
            font=(POLICE, 10, "bold"),
        )
        style.map(
            "Accent.TButton",
            background=[
                ("disabled", COULEUR_FOND_CHAMP),
                ("pressed", COULEUR_ACCENT_ACTIF),
                ("active", COULEUR_ACCENT_HOVER),
            ],
            foreground=[("disabled", COULEUR_TEXTE_ATTENUE)],
        )

        # Bouton d'action destructive (purge) : même gabarit que TButton,
        # texte rouge pour signaler le danger sans casser la palette.
        style.configure(
            "Danger.TButton",
            background=COULEUR_FOND_CHAMP,
            foreground=COULEUR_DANGER,
            borderwidth=0,
            focuscolor=COULEUR_FOND_CHAMP,
            padding=(12, 7),
            font=(POLICE, 9),
        )
        style.map(
            "Danger.TButton",
            background=[("active", COULEUR_BORDURE), ("disabled", COULEUR_FOND_PANNEAU)],
            foreground=[("active", COULEUR_DANGER_HOVER), ("disabled", COULEUR_TEXTE_ATTENUE)],
        )

        style.configure(
            "TCheckbutton",
            background=COULEUR_FOND_PANNEAU,
            foreground=COULEUR_TEXTE,
            indicatorbackground=COULEUR_FOND_CHAMP,
            indicatorforeground=COULEUR_ACCENT,
            font=(POLICE, 9),
        )
        style.map(
            "TCheckbutton",
            background=[("active", COULEUR_FOND_PANNEAU)],
            indicatorbackground=[("selected", COULEUR_ACCENT), ("active", COULEUR_BORDURE)],
        )

        style.configure(
            "TEntry",
            fieldbackground=COULEUR_FOND_CHAMP,
            foreground=COULEUR_TEXTE,
            insertcolor=COULEUR_TEXTE,
            bordercolor=COULEUR_BORDURE,
            lightcolor=COULEUR_FOND_CHAMP,
            darkcolor=COULEUR_FOND_CHAMP,
            borderwidth=1,
            padding=(8, 5),
        )
        style.map(
            "TEntry",
            bordercolor=[("focus", COULEUR_ACCENT)],
            lightcolor=[("focus", COULEUR_FOND_CHAMP)],
            darkcolor=[("focus", COULEUR_FOND_CHAMP)],
        )

        style.configure(
            "TCombobox",
            fieldbackground=COULEUR_FOND_CHAMP,
            background=COULEUR_FOND_CHAMP,
            foreground=COULEUR_TEXTE,
            arrowcolor=COULEUR_TEXTE_ATTENUE,
            bordercolor=COULEUR_BORDURE,
            lightcolor=COULEUR_FOND_CHAMP,
            darkcolor=COULEUR_FOND_CHAMP,
            padding=(8, 5),
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", COULEUR_FOND_CHAMP)],
            bordercolor=[("focus", COULEUR_ACCENT)],
            arrowcolor=[("active", COULEUR_ACCENT_HOVER)],
        )
        # Le menu déroulant d'un Combobox est un tk.Listbox brut, pas stylable
        # via ttk.Style : on le fixe globalement via les options Tk classiques.
        self.option_add("*TCombobox*Listbox.background", COULEUR_FOND_CHAMP)
        self.option_add("*TCombobox*Listbox.foreground", COULEUR_TEXTE)
        self.option_add("*TCombobox*Listbox.selectBackground", COULEUR_ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.option_add("*TCombobox*Listbox.font", (POLICE, 9))

        style.configure(
            "Vertical.TScrollbar",
            background=COULEUR_FOND_CHAMP,
            troughcolor=COULEUR_FOND_PANNEAU,
            borderwidth=0,
            arrowcolor=COULEUR_TEXTE_ATTENUE,
        )
        style.map("Vertical.TScrollbar", background=[("active", COULEUR_ACCENT)])

        style.configure("TSeparator", background=COULEUR_BORDURE)

        # Barre de progression sobre pour la récupération des vidéos (pas de
        # fenêtre système, juste un fin liseré directement dans l'interface).
        style.configure(
            "Recuperation.Horizontal.TProgressbar",
            background=COULEUR_ACCENT,
            troughcolor=COULEUR_FOND_CHAMP,
            borderwidth=0,
            thickness=6,
        )

    def _creer_carte(self, parent, titre):
        """Section visuellement isolée (fond légèrement plus clair, fin liseré)
        façon carte d'interface pro. Retourne le conteneur intérieur (padded,
        style "Panneau") dans lequel construire le contenu de la section."""
        carte = tk.Frame(
            parent,
            bg=COULEUR_FOND_PANNEAU,
            highlightthickness=1,
            highlightbackground=COULEUR_BORDURE,
            highlightcolor=COULEUR_BORDURE,
        )
        carte.pack(fill="x", pady=(0, 14))

        interieur = ttk.Frame(carte, style="Panneau.TFrame", padding=18)
        interieur.pack(fill="both", expand=True)

        ttk.Label(interieur, text=titre.upper(), style="TitreCarte.TLabel").pack(
            anchor="w", pady=(0, 12)
        )
        return interieur

    def _construire_interface(self):
        conteneur = ttk.Frame(self, padding=20)
        conteneur.pack(fill="both", expand=True)

        # --- En-tête : marque + titre + version, bouton Paramètres --------
        entete = ttk.Frame(conteneur)
        entete.pack(fill="x", pady=(0, 4))

        marque = tk.Frame(entete, bg=COULEUR_ACCENT, width=38, height=38)
        marque.pack(side="left")
        marque.pack_propagate(False)
        tk.Label(
            marque, text="MB", bg=COULEUR_ACCENT, fg="#ffffff", font=("Segoe UI", 12, "bold")
        ).pack(expand=True)

        zone_titre = ttk.Frame(entete)
        zone_titre.pack(side="left", padx=(12, 0))
        ttk.Label(zone_titre, text="MarkerBridge", style="Titre.TLabel").pack(anchor="w")
        ttk.Label(
            zone_titre,
            text=f"Chapitres OBS → marqueurs Premiere · v{core.VERSION}",
            style="Attenue.TLabel",
        ).pack(anchor="w")

        ttk.Button(entete, text="⚙ Paramètres", command=self._ouvrir_parametres).pack(
            side="right", anchor="n"
        )

        ttk.Separator(conteneur, orient="horizontal").pack(fill="x", pady=(14, 16))

        # --- Carte : récupération des vidéos depuis le PC de stream -------
        carte_stream = self._creer_carte(conteneur, "Récupération depuis le PC de stream")

        ligne_source = ttk.Frame(carte_stream, style="Panneau.TFrame")
        ligne_source.pack(fill="x", pady=(0, 6))
        ttk.Label(
            ligne_source, text="Dossier source (PC de stream) :", style="Panneau.TLabel", width=26
        ).pack(side="left")
        self.var_dossier_source_stream = tk.StringVar(
            value=self.config.get("dossier_source_stream", "")
        )
        ttk.Entry(ligne_source, textvariable=self.var_dossier_source_stream).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(
            ligne_source, text="Parcourir...", command=self._choisir_dossier_source_stream
        ).pack(side="left", padx=(8, 0))

        ligne_reception = ttk.Frame(carte_stream, style="Panneau.TFrame")
        ligne_reception.pack(fill="x", pady=(0, 6))
        ttk.Label(
            ligne_reception, text="Dossier de réception (ce PC) :", style="Panneau.TLabel", width=26
        ).pack(side="left")
        self.var_dossier_reception = tk.StringVar(
            value=self.config.get("dossier_reception", "")
        )
        ttk.Entry(ligne_reception, textvariable=self.var_dossier_reception).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(
            ligne_reception, text="Parcourir...", command=self._choisir_dossier_reception
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            ligne_reception, text="Ouvrir", command=self._ouvrir_dossier_reception
        ).pack(side="left", padx=(8, 0))

        ligne_option_deplacer = ttk.Frame(carte_stream, style="Panneau.TFrame")
        ligne_option_deplacer.pack(fill="x", pady=(8, 10))

        self.var_deplacer_apres_copie = tk.BooleanVar(
            value=self.config.get("deplacer_apres_copie", True)
        )
        ttk.Checkbutton(
            ligne_option_deplacer,
            text="Supprimer les vidéos du PC de stream une fois copiées (déplacer)",
            variable=self.var_deplacer_apres_copie,
        ).pack(side="left")

        ligne_actions_stream = ttk.Frame(carte_stream, style="Panneau.TFrame")
        ligne_actions_stream.pack(fill="x")

        self.bouton_recuperer = ttk.Button(
            ligne_actions_stream,
            text="Récupérer les nouvelles vidéos",
            style="Accent.TButton",
            command=self._lancer_recuperation,
        )
        self.bouton_recuperer.pack(side="right")

        self.bouton_purger = ttk.Button(
            ligne_actions_stream,
            text="Purger les dossiers...",
            style="Danger.TButton",
            command=self._lancer_purge,
        )
        self.bouton_purger.pack(side="right", padx=(0, 8))

        # Progression de la récupération : masquée tant qu'aucune récupération
        # n'est en cours, pour rester sobre le reste du temps.
        self.zone_progression_recuperation = ttk.Frame(carte_stream, style="Panneau.TFrame")

        self.barre_progression_recuperation = ttk.Progressbar(
            self.zone_progression_recuperation,
            style="Recuperation.Horizontal.TProgressbar",
            orient="horizontal",
            mode="determinate",
        )
        self.barre_progression_recuperation.pack(fill="x", pady=(10, 4))

        self.label_progression_recuperation = ttk.Label(
            self.zone_progression_recuperation, text="", style="PanneauAttenue.TLabel"
        )
        self.label_progression_recuperation.pack(anchor="w")

        # --- Carte : fichiers à convertir ----------------------------------
        carte_fichiers = self._creer_carte(conteneur, "Fichiers à convertir")

        barre_boutons = ttk.Frame(carte_fichiers, style="Panneau.TFrame")
        barre_boutons.pack(fill="x", pady=(0, 10))

        # Boutons gardés en attributs pour pouvoir les désactiver pendant une
        # récupération/conversion/purge (modifier la liste à ce moment-là est
        # au mieux sans effet, au pire trompeur).
        self.boutons_liste = []
        for texte, commande in (
            ("Ajouter un dossier...", self._ajouter_dossier),
            ("Ajouter des fichiers...", self._ajouter_fichiers),
            ("Retirer la sélection", self._retirer_selection),
            ("Vider la liste", self._vider_liste),
        ):
            bouton = ttk.Button(barre_boutons, text=texte, command=commande)
            bouton.pack(side="left", padx=(0 if not self.boutons_liste else 8, 0))
            self.boutons_liste.append(bouton)

        zone_liste = ttk.Frame(carte_fichiers, style="Panneau.TFrame")
        zone_liste.pack(fill="both", expand=True)

        defilement = ttk.Scrollbar(zone_liste, orient="vertical")
        self.liste_fichiers = tk.Listbox(
            zone_liste,
            selectmode="extended",
            bg=COULEUR_FOND_CHAMP,
            fg=COULEUR_TEXTE,
            selectbackground=COULEUR_ACCENT,
            selectforeground="#ffffff",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=COULEUR_BORDURE,
            highlightcolor=COULEUR_ACCENT,
            font=("Consolas", 9),
            height=8,
            yscrollcommand=defilement.set,
        )
        defilement.config(command=self.liste_fichiers.yview)
        self.liste_fichiers.pack(side="left", fill="both", expand=True)
        defilement.pack(side="right", fill="y")

        # --- Carte : réglages d'export --------------------------------------
        carte_export = self._creer_carte(conteneur, "Réglages d'export")

        panneau_decalage = ttk.Frame(carte_export, style="Panneau.TFrame")
        panneau_decalage.pack(fill="x", pady=(0, 10))

        ttk.Label(
            panneau_decalage,
            text="Décalage marqueurs (secondes, compense la latence OBS) :",
            style="Panneau.TLabel",
        ).pack(side="left")
        self.var_decalage = tk.StringVar(
            value=str(self.config.get("decalage_sec", core.DECALAGE_PAR_DEFAUT_SEC))
        )
        ttk.Entry(panneau_decalage, textvariable=self.var_decalage, width=8).pack(
            side="left", padx=(8, 0)
        )

        self.var_a_cote_source = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            carte_export,
            text="Exporter les XML à côté de chaque vidéo source",
            variable=self.var_a_cote_source,
            command=self._basculer_mode_export,
        ).pack(anchor="w")

        ligne_dossier = ttk.Frame(carte_export, style="Panneau.TFrame")
        ligne_dossier.pack(fill="x", pady=(8, 0))

        self.var_dossier_export = tk.StringVar(value="")
        self.champ_dossier_export = ttk.Entry(
            ligne_dossier, textvariable=self.var_dossier_export, state="disabled"
        )
        self.champ_dossier_export.pack(side="left", fill="x", expand=True)

        self.bouton_parcourir_export = ttk.Button(
            ligne_dossier,
            text="Parcourir...",
            command=self._choisir_dossier_export,
            state="disabled",
        )
        self.bouton_parcourir_export.pack(side="left", padx=(8, 0))

        # --- Barre de lancement --------------------------------------------
        barre_lancement = ttk.Frame(conteneur)
        barre_lancement.pack(fill="x", pady=(4, 0))

        self.bouton_lancer = ttk.Button(
            barre_lancement,
            text="Lancer la conversion",
            style="Accent.TButton",
            command=self._lancer_conversion,
        )
        self.bouton_lancer.pack(side="left")

        self.label_statut = ttk.Label(barre_lancement, text="", style="Attenue.TLabel")
        self.label_statut.pack(side="left", padx=(12, 0))

    def _construire_fenetre_parametres(self):
        """Fenêtre Paramètres : infos version/GitHub, journal, et futurs réglages.

        Créée une seule fois puis masquée (withdraw) au lieu d'être détruite à
        la fermeture, pour que le journal (self.zone_texte_log) reste vivant et
        continue de recevoir les messages même quand la fenêtre est fermée.
        """
        fenetre = tk.Toplevel(self)
        fenetre.title("Paramètres")
        fenetre.geometry("600x820")
        fenetre.minsize(480, 560)
        fenetre.configure(bg=COULEUR_FOND)
        fenetre.protocol("WM_DELETE_WINDOW", fenetre.withdraw)

        conteneur = ttk.Frame(fenetre, padding=20)
        conteneur.pack(fill="both", expand=True)

        ttk.Label(conteneur, text="Paramètres", style="Titre.TLabel").pack(anchor="w")
        ttk.Separator(conteneur, orient="horizontal").pack(fill="x", pady=(14, 16))

        carte_infos = self._creer_carte(conteneur, "À propos")

        ligne_version = ttk.Frame(carte_infos, style="Panneau.TFrame")
        ligne_version.pack(fill="x")
        ttk.Label(
            ligne_version, text="MarkerBridge", style="Panneau.TLabel"
        ).pack(side="left")
        tk.Label(
            ligne_version,
            text=f" v{core.VERSION} ",
            bg=COULEUR_ACCENT,
            fg="#ffffff",
            font=("Segoe UI", 8, "bold"),
        ).pack(side="left", padx=(8, 0))

        lien_github = ttk.Label(
            carte_infos,
            text=f"🔗 {core.URL_GITHUB_AUTEUR}",
            style="Lien.TLabel",
            cursor="hand2",
        )
        lien_github.pack(anchor="w", pady=(8, 0))
        lien_github.bind(
            "<Button-1>", lambda evenement: webbrowser.open(core.URL_GITHUB_AUTEUR)
        )

        # Renommage automatique : réglable, pas figé sur la convention
        # personnelle d'origine ("Partie N") puisque l'outil est partagé.
        carte_renommage = self._creer_carte(conteneur, "Renommage automatique")

        self.var_renommage_actif = tk.BooleanVar(
            value=self.config.get("renommage_actif", True)
        )
        ttk.Checkbutton(
            carte_renommage,
            text="Renommer automatiquement les vidéos après récupération",
            variable=self.var_renommage_actif,
            command=self._sauvegarder_config,
        ).pack(anchor="w")

        ligne_modele = ttk.Frame(carte_renommage, style="Panneau.TFrame")
        ligne_modele.pack(fill="x", pady=(10, 0))
        ttk.Label(ligne_modele, text="Règle de renommage :", style="Panneau.TLabel").pack(
            side="left"
        )
        self.var_modele_renommage = tk.StringVar(
            value=self.config.get("modele_renommage", MODELE_RENOMMAGE_PAR_DEFAUT)
        )
        self.champ_modele_renommage = ttk.Combobox(
            ligne_modele,
            textvariable=self.var_modele_renommage,
            values=MODELES_RENOMMAGE_PREDEFINIS,
        )
        self.champ_modele_renommage.pack(side="left", padx=(8, 0), fill="x", expand=True)
        self.champ_modele_renommage.bind(
            "<<ComboboxSelected>>", lambda evenement: self._sauvegarder_config()
        )
        self.champ_modele_renommage.bind(
            "<FocusOut>", lambda evenement: self._sauvegarder_config()
        )

        ttk.Label(
            carte_renommage,
            text=(
                "Choisissez une règle prédéfinie ou tapez la vôtre. Espaces réservés : "
                "{n} numéro auto-incrémenté (obligatoire), {date} AAAA-MM-JJ, {heure} HH-MM-SS."
            ),
            style="PanneauAttenue.TLabel",
            wraplength=460,
            justify="left",
        ).pack(anchor="w", pady=(8, 0))

        ttk.Label(conteneur, text="JOURNAL", style="TitreCarte.TLabel").pack(
            anchor="w", pady=(4, 6)
        )

        carte_log = tk.Frame(
            conteneur,
            bg=COULEUR_FOND_PANNEAU,
            highlightthickness=1,
            highlightbackground=COULEUR_BORDURE,
            highlightcolor=COULEUR_BORDURE,
        )
        carte_log.pack(fill="both", expand=True)

        zone_log = ttk.Frame(carte_log, style="Panneau.TFrame", padding=4)
        zone_log.pack(fill="both", expand=True)

        defilement_log = ttk.Scrollbar(zone_log, orient="vertical")
        self.zone_texte_log = tk.Text(
            zone_log,
            bg=COULEUR_FOND_PANNEAU,
            fg=COULEUR_TEXTE,
            insertbackground=COULEUR_TEXTE,
            borderwidth=0,
            highlightthickness=0,
            font=("Consolas", 9),
            state="disabled",
            wrap="word",
            yscrollcommand=defilement_log.set,
        )
        defilement_log.config(command=self.zone_texte_log.yview)
        self.zone_texte_log.pack(side="left", fill="both", expand=True)
        defilement_log.pack(side="right", fill="y")

        fenetre.withdraw()
        return fenetre

    def _ouvrir_parametres(self):
        self.fenetre_parametres.deiconify()
        self.fenetre_parametres.lift()
        self.fenetre_parametres.focus_force()

    # ---------------------------------------------------- récupération stream

    def _choisir_dossier_source_stream(self):
        dossier = filedialog.askdirectory(
            title="Choisir le dossier d'enregistrement du PC de stream (chemin réseau)"
        )
        if dossier:
            self.var_dossier_source_stream.set(dossier)
            self._sauvegarder_config()

    def _choisir_dossier_reception(self):
        dossier = filedialog.askdirectory(title="Choisir le dossier de réception sur ce PC")
        if dossier:
            self.var_dossier_reception.set(dossier)
            self._sauvegarder_config()

    def _ouvrir_dossier_reception(self):
        """Ouvre le dossier de réception dans l'Explorateur Windows."""
        dossier = self.var_dossier_reception.get().strip()
        if not dossier or not os.path.isdir(dossier):
            self._informer_sombre(
                "Dossier introuvable",
                "Le dossier de réception n'est pas renseigné ou n'existe pas encore.",
            )
            return
        os.startfile(os.path.normpath(dossier))

    def _lancer_recuperation(self):
        if self.recuperation_en_cours or self.conversion_en_cours or self.purge_en_cours:
            return

        source = self.var_dossier_source_stream.get().strip()
        destination = self.var_dossier_reception.get().strip()

        if not source or not destination:
            self._informer_sombre(
                "Chemins manquants",
                "Renseignez le dossier source (PC de stream) et le dossier de réception.",
            )
            return

        # L'accès au dossier source (partage réseau) est vérifié DANS le thread :
        # un partage SMB injoignable peut mettre plusieurs secondes à répondre,
        # ce qui gèlerait l'interface si on testait ici.
        os.makedirs(destination, exist_ok=True)
        self._sauvegarder_config()

        self.recuperation_en_cours = True
        self._activer_ui(False)
        self.label_statut.config(text="Vérification de l'accès au PC de stream...")

        self.barre_progression_recuperation.config(value=0, maximum=1)
        self.label_progression_recuperation.config(text="Recherche des vidéos à récupérer...")
        self.zone_progression_recuperation.pack(fill="x", pady=(0, 0))

        deplacer = self.var_deplacer_apres_copie.get()
        renommage_actif = self.var_renommage_actif.get()
        modele_renommage = self.var_modele_renommage.get().strip() or MODELE_RENOMMAGE_PAR_DEFAUT
        if renommage_actif:
            # Sans {n}, la boucle anti-collision de _renommer_en_partie ne
            # changerait jamais de nom et tournerait sans fin.
            if "{n}" not in modele_renommage:
                modele_renommage = MODELE_RENOMMAGE_PAR_DEFAUT
                self.var_modele_renommage.set(modele_renommage)
                self._journaliser(
                    "Règle de renommage invalide (doit contenir {n}), valeur par défaut "
                    f"'{MODELE_RENOMMAGE_PAR_DEFAUT}' utilisée."
                )
            # Caractères interdits dans un nom de fichier Windows : retirés
            # silencieusement du modèle, sinon chaque renommage échouerait.
            modele_nettoye = re.sub(r'[<>:"/\\|?*]', "", modele_renommage)
            if modele_nettoye != modele_renommage:
                modele_renommage = modele_nettoye or MODELE_RENOMMAGE_PAR_DEFAUT
                self.var_modele_renommage.set(modele_renommage)
                self._journaliser(
                    "Caractères interdits dans un nom de fichier Windows retirés de "
                    f"la règle de renommage : '{modele_renommage}'."
                )
            self._sauvegarder_config()

        thread = threading.Thread(
            target=self._travail_recuperation,
            args=(source, destination, deplacer, renommage_actif, modele_renommage),
            daemon=True,
        )
        thread.start()

    # Nombre de tentatives par fichier en cas d'erreur réseau transitoire
    # (partage SMB qui se déconnecte/reconnecte brièvement), et délai entre elles.
    NB_TENTATIVES_COPIE = 3
    DELAI_ENTRE_TENTATIVES_SEC = 2

    # Un fichier modifié il y a moins de N secondes est probablement encore en
    # cours d'enregistrement par OBS : le copier maintenant donnerait une copie
    # tronquée (et le mode "déplacer" supprimerait l'original incomplet !).
    AGE_MINIMUM_FICHIER_SEC = 30

    def _copier_un_fichier_avec_reprise(self, chemin_source, chemin_destination):
        """Copie un fichier avec reprise automatique sur erreur réseau transitoire.

        Retourne (succes: bool, message_erreur: str | None).
        """
        derniere_erreur = None
        for tentative in range(1, self.NB_TENTATIVES_COPIE + 1):
            try:
                shutil.copy2(chemin_source, chemin_destination)

                taille_source = os.path.getsize(chemin_source)
                taille_destination = os.path.getsize(chemin_destination)
                if taille_source != taille_destination:
                    raise IOError(
                        "taille différente après copie (copie potentiellement corrompue)"
                    )
                return True, None
            except OSError as erreur:
                derniere_erreur = erreur
                # Copie partielle/ratée : on la retire avant de retenter, pour ne pas
                # fausser la comparaison de taille au prochain essai.
                if os.path.exists(chemin_destination):
                    try:
                        os.remove(chemin_destination)
                    except OSError:
                        pass

                if tentative < self.NB_TENTATIVES_COPIE:
                    self._journaliser_depuis_thread(
                        f"  Échec (tentative {tentative}/{self.NB_TENTATIVES_COPIE}), "
                        f"nouvelle tentative dans {self.DELAI_ENTRE_TENTATIVES_SEC}s : {erreur}"
                    )
                    time.sleep(self.DELAI_ENTRE_TENTATIVES_SEC)

        return False, str(derniere_erreur)

    # Renommage automatique après récupération : configurable depuis les
    # Paramètres (règle prédéfinie ou personnalisée), pas figé sur "Partie N"
    # qui n'était que la convention personnelle d'origine.

    def _motif_depuis_modele(self, modele):
        """Construit une regex qui reconnaît les fichiers déjà nommés selon ce
        modèle, pour reprendre la numérotation là où elle s'est arrêtée plutôt
        que de repartir de 1 à chaque récupération. Seul {n} est capturé (le
        numéro) ; {date}/{heure} sont traités comme des jokers non capturants.
        """
        motif = re.escape(modele)
        motif = motif.replace(re.escape("{n}"), r"(\d+)")
        motif = motif.replace(re.escape("{date}"), r"[^\\/]+")
        motif = motif.replace(re.escape("{heure}"), r"[^\\/]+")
        if not motif.lower().endswith(re.escape(".mp4")):
            motif += re.escape(".mp4")
        return re.compile("^" + motif + "$", re.IGNORECASE)

    def _nom_depuis_modele(self, modele, numero):
        """Applique le modèle de renommage (espaces réservés {n}/{date}/{heure})."""
        maintenant = datetime.datetime.now()
        nom = modele.replace("{n}", str(numero))
        nom = nom.replace("{date}", maintenant.strftime("%Y-%m-%d"))
        nom = nom.replace("{heure}", maintenant.strftime("%H-%M-%S"))
        if not nom.lower().endswith(".mp4"):
            nom += ".mp4"
        return nom

    def _prochain_numero_partie(self, destination, modele):
        """Cherche le plus grand numéro déjà présent dans le dossier pour cette
        règle de renommage, pour continuer la numérotation au lieu de repartir
        de 1 à chaque fois."""
        plus_grand = 0
        motif = self._motif_depuis_modele(modele)
        try:
            for nom in os.listdir(destination):
                correspondance = motif.match(nom)
                if correspondance and correspondance.groups():
                    plus_grand = max(plus_grand, int(correspondance.group(1)))
        except OSError:
            pass
        return plus_grand + 1

    def _renommer_en_partie(self, chemin_actuel, destination, modele, numero):
        """Renomme le fichier récupéré selon le modèle choisi, sans écraser un
        fichier existant."""
        while True:
            nom_genere = self._nom_depuis_modele(modele, numero)
            chemin_cible = os.path.join(destination, nom_genere)
            if not os.path.exists(chemin_cible):
                break
            numero += 1

        try:
            os.rename(chemin_actuel, chemin_cible)
            return chemin_cible, numero + 1
        except OSError as erreur:
            self._journaliser_depuis_thread(
                f"  Attention : renommage en '{nom_genere}' échoué ({erreur}), "
                "nom d'origine conservé."
            )
            return chemin_actuel, numero

    def _travail_recuperation(self, source, destination, deplacer, renommage_actif, modele_renommage):
        source = os.path.normpath(source)
        destination = os.path.normpath(destination)

        # Test d'accès fait ici (et pas avant de lancer le thread) : sur un
        # partage réseau injoignable, isdir peut bloquer plusieurs secondes.
        if not os.path.isdir(source):
            self.file_attente.put((
                "erreur",
                (
                    "Dossier source introuvable",
                    f"Impossible d'accéder à :\n{source}\n\n"
                    "Vérifiez que le PC de stream est allumé et que le partage "
                    "réseau est accessible.",
                ),
            ))
            self.file_attente.put(("fin_recuperation", []))
            return

        try:
            fichiers_source = core.lister_fichiers_mp4(source)
        except ValueError as erreur:
            self._journaliser_depuis_thread(f"ERREUR : {erreur}")
            self.file_attente.put(("fin_recuperation", []))
            return

        nouveaux_fichiers = []
        nb_ignores = 0
        nb_echecs = 0
        nb_en_cours_enregistrement = 0
        total_fichiers = len(fichiers_source)
        prochain_numero_partie = (
            self._prochain_numero_partie(destination, modele_renommage)
            if renommage_actif
            else None
        )
        for index, chemin_source in enumerate(fichiers_source, start=1):
            nom_fichier = os.path.basename(chemin_source)
            self.file_attente.put(
                ("progress_recuperation", (index, total_fichiers, nom_fichier))
            )
            chemin_destination = os.path.normpath(os.path.join(destination, nom_fichier))

            if os.path.exists(chemin_destination):
                nb_ignores += 1
                continue

            # Fichier trop récent = probablement encore en cours d'enregistrement
            # par OBS : on le laisse tranquille, il sera récupéré au prochain passage.
            try:
                age_sec = time.time() - os.path.getmtime(chemin_source)
            except OSError:
                age_sec = None
            if age_sec is not None and age_sec < self.AGE_MINIMUM_FICHIER_SEC:
                nb_en_cours_enregistrement += 1
                self._journaliser_depuis_thread(
                    f"{nom_fichier} modifié il y a {age_sec:.0f}s : probablement encore "
                    "en cours d'enregistrement, ignoré pour cette fois."
                )
                continue

            self._journaliser_depuis_thread(f"Copie de {nom_fichier} ...")
            succes, message_erreur = self._copier_un_fichier_avec_reprise(
                chemin_source, chemin_destination
            )

            if not succes:
                nb_echecs += 1
                self._journaliser_depuis_thread(
                    f"  ERREUR sur {nom_fichier} après {self.NB_TENTATIVES_COPIE} tentative(s) : "
                    f"{message_erreur}"
                )
                continue

            if deplacer:
                try:
                    os.remove(chemin_source)
                    self._journaliser_depuis_thread(f"  -> {nom_fichier} copié puis supprimé de la source.")
                except OSError as erreur:
                    # La copie a réussi : on garde le fichier récupéré même si la
                    # suppression de la source échoue (ex: verrou réseau persistant).
                    self._journaliser_depuis_thread(
                        f"  -> {nom_fichier} copié, mais suppression de la source échouée : {erreur}"
                    )
            else:
                self._journaliser_depuis_thread(f"  -> {nom_fichier} copié.")

            if renommage_actif:
                chemin_final, prochain_numero_partie = self._renommer_en_partie(
                    chemin_destination, destination, modele_renommage, prochain_numero_partie
                )
                if chemin_final != chemin_destination:
                    self._journaliser_depuis_thread(
                        f"  -> renommé en {os.path.basename(chemin_final)}"
                    )
            else:
                chemin_final = chemin_destination

            nouveaux_fichiers.append(chemin_final)

        if nb_ignores:
            self._journaliser_depuis_thread(
                f"{nb_ignores} fichier(s) déjà présent(s) côté réception, ignoré(s)."
            )
        if nb_en_cours_enregistrement:
            self._journaliser_depuis_thread(
                f"{nb_en_cours_enregistrement} fichier(s) probablement encore en cours "
                "d'enregistrement : relancez la récupération une fois l'enregistrement "
                "OBS arrêté."
            )
        if nb_echecs:
            self._journaliser_depuis_thread(
                f"{nb_echecs} fichier(s) n'ont pas pu être récupérés (voir erreurs ci-dessus). "
                "Ils sont toujours sur le PC de stream : relancez la récupération pour réessayer."
            )
        self._journaliser_depuis_thread(
            f"\nRécupération terminée : {len(nouveaux_fichiers)} nouveau(x) fichier(s) récupéré(s)."
        )
        self.file_attente.put(("fin_recuperation", nouveaux_fichiers))

    def _progression_recuperation(self, progression):
        index, total, nom_fichier = progression
        self.barre_progression_recuperation.config(maximum=max(total, 1), value=index)
        self.label_progression_recuperation.config(
            text=f"Fichier {index}/{total} : {nom_fichier}"
        )

    def _fin_recuperation(self, nouveaux_fichiers):
        self.recuperation_en_cours = False
        self._activer_ui(True)
        self.label_statut.config(text="Récupération terminée.")
        self.zone_progression_recuperation.pack_forget()

        ajoutes = 0
        for chemin in nouveaux_fichiers:
            if chemin not in self.fichiers_mp4:
                self.fichiers_mp4.append(chemin)
                ajoutes += 1
        if ajoutes:
            self._rafraichir_liste()
            self._journaliser(f"{ajoutes} nouveau(x) fichier(s) ajouté(s) à la liste de conversion.")

    # ------------------------------------------------- boîtes de dialogue

    def _confirmer_sombre(self, titre, message, texte_oui="Oui", texte_non="Annuler", danger=False):
        """Boîte de confirmation oui/non modale au thème de l'application,
        à la place des messagebox système (qui cassent l'ambiance sombre).

        Retourne True si l'utilisateur confirme, False sinon (bouton Annuler,
        fermeture de la fenêtre ou touche Échap). Entrée valide le choix par
        défaut : Annuler (jamais l'action destructive)."""
        boite = tk.Toplevel(self)
        boite.title(titre)
        boite.configure(bg=COULEUR_FOND)
        boite.resizable(False, False)
        boite.transient(self)

        resultat = {"valeur": False}

        conteneur = ttk.Frame(boite, padding=20)
        conteneur.pack(fill="both", expand=True)

        ttk.Label(conteneur, text=titre, style="Titre.TLabel").pack(anchor="w")
        ttk.Label(
            conteneur, text=message, justify="left", wraplength=440
        ).pack(anchor="w", pady=(10, 18))

        barre_boutons = ttk.Frame(conteneur)
        barre_boutons.pack(fill="x")

        def valider():
            resultat["valeur"] = True
            boite.destroy()

        bouton_oui = ttk.Button(
            barre_boutons,
            text=texte_oui,
            style="Danger.TButton" if danger else "Accent.TButton",
            command=valider,
        )
        bouton_oui.pack(side="right")
        bouton_non = ttk.Button(barre_boutons, text=texte_non, command=boite.destroy)
        bouton_non.pack(side="right", padx=(0, 8))

        boite.bind("<Escape>", lambda evenement: boite.destroy())
        boite.bind("<Return>", lambda evenement: boite.destroy())
        bouton_non.focus_set()

        # Centre la boîte sur la fenêtre principale.
        boite.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - boite.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - boite.winfo_reqheight()) // 3
        boite.geometry(f"+{max(x, 0)}+{max(y, 0)}")

        boite.grab_set()
        self.wait_window(boite)
        return resultat["valeur"]

    def _informer_sombre(self, titre, message):
        """Boîte d'information/erreur modale au thème de l'application (un seul
        bouton OK), remplaçant messagebox.showwarning/showerror système."""
        boite = tk.Toplevel(self)
        boite.title(titre)
        boite.configure(bg=COULEUR_FOND)
        boite.resizable(False, False)
        boite.transient(self)

        conteneur = ttk.Frame(boite, padding=20)
        conteneur.pack(fill="both", expand=True)

        ttk.Label(conteneur, text=titre, style="Titre.TLabel").pack(anchor="w")
        ttk.Label(
            conteneur, text=message, justify="left", wraplength=440
        ).pack(anchor="w", pady=(10, 18))

        bouton_ok = ttk.Button(
            conteneur, text="OK", style="Accent.TButton", command=boite.destroy
        )
        bouton_ok.pack(anchor="e")

        boite.bind("<Escape>", lambda evenement: boite.destroy())
        boite.bind("<Return>", lambda evenement: boite.destroy())
        bouton_ok.focus_set()

        boite.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - boite.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - boite.winfo_reqheight()) // 3
        boite.geometry(f"+{max(x, 0)}+{max(y, 0)}")

        boite.grab_set()
        self.wait_window(boite)

    # -------------------------------------------------------------- purge

    def _purge_refusee(self, chemin):
        """Garde-fou : retourne le motif du refus si le chemin est trop dangereux
        à purger (racine de disque, dossier utilisateur, Bureau/Documents/Vidéos...),
        sinon None. Évite qu'une mauvaise saisie vide la moitié du PC."""
        chemin_abs = os.path.normcase(os.path.normpath(os.path.abspath(chemin)))
        disque, reste = os.path.splitdrive(chemin_abs)
        composants = [c for c in reste.replace("\\", "/").split("/") if c]

        if not composants:
            if disque.startswith("\\\\"):
                return "la racine d'un partage réseau"
            return "la racine d'un disque"

        profil = os.path.normcase(os.path.normpath(os.path.expanduser("~")))
        if chemin_abs == profil:
            return "le dossier utilisateur"
        if os.path.dirname(chemin_abs) == profil:
            return "un dossier standard du profil (Bureau, Documents, Vidéos...)"

        return None

    def _lancer_purge(self):
        if self.recuperation_en_cours or self.conversion_en_cours or self.purge_en_cours:
            return

        source = self.var_dossier_source_stream.get().strip()
        destination = self.var_dossier_reception.get().strip()

        if not source and not destination:
            self._informer_sombre(
                "Chemins manquants",
                "Renseignez au moins le dossier source ou le dossier de réception "
                "avant de purger.",
            )
            return

        dossiers_a_purger = []
        for dossier in (source, destination):
            if dossier and os.path.isdir(dossier) and dossier not in dossiers_a_purger:
                motif_refus = self._purge_refusee(dossier)
                if motif_refus:
                    self._informer_sombre(
                        "Purge refusée",
                        f"Le dossier suivant ne sera pas purgé car c'est {motif_refus} :\n\n"
                        f"{dossier}\n\n"
                        "Choisissez un sous-dossier dédié aux enregistrements.",
                    )
                    continue
                dossiers_a_purger.append(dossier)

        if not dossiers_a_purger:
            self._informer_sombre(
                "Dossiers introuvables",
                "Aucun des dossiers renseignés n'est accessible (ou autorisé) "
                "actuellement.",
            )
            return

        liste_dossiers = "\n".join(f"•  {dossier}" for dossier in dossiers_a_purger)
        confirmation = self._confirmer_sombre(
            "Purger les dossiers ?",
            "Cette action supprime définitivement tout le contenu des dossiers "
            "suivants :\n\n"
            f"{liste_dossiers}\n\n"
            "C'est irréversible (vidéos comprises).",
            texte_oui="Oui, tout supprimer",
            danger=True,
        )
        if not confirmation:
            self._journaliser("Purge annulée.")
            return

        self.purge_en_cours = True
        self._activer_ui(False)
        self.label_statut.config(text="Purge des dossiers en cours...")

        thread = threading.Thread(
            target=self._travail_purge, args=(dossiers_a_purger,), daemon=True
        )
        thread.start()

    def _travail_purge(self, dossiers):
        total_supprimes = 0
        total_echecs = 0
        for dossier in dossiers:
            self._journaliser_depuis_thread(f"Purge de {dossier} ...")
            try:
                noms = os.listdir(dossier)
            except OSError as erreur:
                self._journaliser_depuis_thread(f"  ERREUR lecture du dossier : {erreur}")
                continue

            for nom in noms:
                chemin = os.path.join(dossier, nom)
                try:
                    if os.path.isdir(chemin):
                        shutil.rmtree(chemin)
                    else:
                        os.remove(chemin)
                    total_supprimes += 1
                except OSError as erreur:
                    total_echecs += 1
                    self._journaliser_depuis_thread(f"  ERREUR suppression de {nom} : {erreur}")

        message_fin = f"\nPurge terminée : {total_supprimes} élément(s) supprimé(s)"
        message_fin += f", {total_echecs} échec(s)." if total_echecs else "."
        self._journaliser_depuis_thread(message_fin)
        self.file_attente.put(("fin_purge", None))

    def _fin_purge(self):
        self.purge_en_cours = False
        self._activer_ui(True)
        self.label_statut.config(text="Purge terminée.")

        # Les fichiers de la liste de conversion qui viennent d'être supprimés
        # n'ont plus rien à convertir : on les retire silencieusement.
        restants = [chemin for chemin in self.fichiers_mp4 if os.path.exists(chemin)]
        if len(restants) != len(self.fichiers_mp4):
            self.fichiers_mp4 = restants
            self._rafraichir_liste()

    # -------------------------------------------------------- gestion liste

    def _rafraichir_liste(self):
        self.liste_fichiers.delete(0, "end")
        for chemin in self.fichiers_mp4:
            self.liste_fichiers.insert("end", chemin)

    def _ajouter_dossier(self):
        dossier = filedialog.askdirectory(title="Choisir un dossier contenant des .mp4")
        if not dossier:
            return
        try:
            fichiers = core.lister_fichiers_mp4(dossier)
        except ValueError as erreur:
            self._informer_sombre("Aucun fichier", str(erreur))
            return

        ajoutes = 0
        for chemin in fichiers:
            if chemin not in self.fichiers_mp4:
                self.fichiers_mp4.append(chemin)
                ajoutes += 1
        self._rafraichir_liste()
        self._journaliser(f"{ajoutes} fichier(s) ajouté(s) depuis le dossier : {dossier}")

    def _ajouter_fichiers(self):
        fichiers = filedialog.askopenfilenames(
            title="Choisir des fichiers .mp4", filetypes=[("Fichiers vidéo MP4", "*.mp4")]
        )
        ajoutes = 0
        for chemin in fichiers:
            if chemin not in self.fichiers_mp4:
                self.fichiers_mp4.append(chemin)
                ajoutes += 1
        self._rafraichir_liste()
        if ajoutes:
            self._journaliser(f"{ajoutes} fichier(s) ajouté(s).")

    def _retirer_selection(self):
        indices = list(self.liste_fichiers.curselection())
        for indice in reversed(indices):
            del self.fichiers_mp4[indice]
        self._rafraichir_liste()

    def _vider_liste(self):
        self.fichiers_mp4.clear()
        self._rafraichir_liste()

    # --------------------------------------------------------- dossier export

    def _basculer_mode_export(self):
        if self.var_a_cote_source.get():
            self.champ_dossier_export.config(state="disabled")
            self.bouton_parcourir_export.config(state="disabled")
        else:
            self.champ_dossier_export.config(state="normal")
            self.bouton_parcourir_export.config(state="normal")

    def _choisir_dossier_export(self):
        dossier = filedialog.askdirectory(title="Choisir le dossier d'export pour Premiere")
        if dossier:
            self.var_dossier_export.set(dossier)

    # ------------------------------------------------------------- journal

    def _journaliser(self, message):
        self.zone_texte_log.config(state="normal")
        self.zone_texte_log.insert("end", message + "\n")
        self.zone_texte_log.see("end")
        self.zone_texte_log.config(state="disabled")

    def _journaliser_depuis_thread(self, message):
        self.file_attente.put(("log", message))

    def _traiter_file_attente(self):
        try:
            while True:
                type_message, contenu = self.file_attente.get_nowait()
                if type_message == "log":
                    self._journaliser(contenu)
                elif type_message == "fin":
                    self._fin_conversion()
                elif type_message == "fin_recuperation":
                    self._fin_recuperation(contenu)
                elif type_message == "progress_recuperation":
                    self._progression_recuperation(contenu)
                elif type_message == "fin_purge":
                    self._fin_purge()
                elif type_message == "erreur":
                    titre, message = contenu
                    self._informer_sombre(titre, message)
        except queue.Empty:
            pass
        self.after(100, self._traiter_file_attente)

    # --------------------------------------------------------- conversion

    def _activer_boutons_liste(self, actif):
        etat = "normal" if actif else "disabled"
        for bouton in self.boutons_liste:
            bouton.config(state=etat)

    def _activer_ui(self, actif):
        etat = "normal" if actif else "disabled"
        self.bouton_lancer.config(state=etat)
        self.bouton_recuperer.config(state=etat)
        self.bouton_purger.config(state=etat)
        self._activer_boutons_liste(actif)

    def _lancer_conversion(self):
        if self.conversion_en_cours or self.recuperation_en_cours or self.purge_en_cours:
            return

        if not self.fichiers_mp4:
            self._informer_sombre("Aucun fichier", "Ajoutez au moins un fichier .mp4.")
            return

        if not self.var_a_cote_source.get() and not self.var_dossier_export.get().strip():
            self._informer_sombre(
                "Dossier d'export manquant",
                "Choisissez un dossier d'export, ou cochez "
                "\"Exporter à côté de chaque vidéo source\".",
            )
            return

        try:
            decalage_sec = float(self.var_decalage.get().strip().replace(",", "."))
        except ValueError:
            self._informer_sombre(
                "Décalage invalide",
                "Le décalage doit être un nombre (ex: 0.75). Utilisation de la valeur "
                f"par défaut ({core.DECALAGE_PAR_DEFAUT_SEC}s).",
            )
            decalage_sec = core.DECALAGE_PAR_DEFAUT_SEC
            self.var_decalage.set(str(core.DECALAGE_PAR_DEFAUT_SEC))

        chemin_ffprobe = core.trouver_ffprobe()
        if chemin_ffprobe is None:
            veut_installer = self._confirmer_sombre(
                "ffmpeg introuvable",
                "ffprobe/ffmpeg est introuvable sur ce système.\n\n"
                "Télécharger automatiquement une version portable "
                "(nécessite Internet, une seule fois) ?",
                texte_oui="Télécharger",
                texte_non="Annuler",
            )
            if not veut_installer:
                self._journaliser(
                    "Installation annulée. Installez ffmpeg manuellement, "
                    f"voir {core.URL_FFMPEG_PORTABLE}"
                )
                return
            self._demarrer_conversion(installation_requise=True, decalage_sec=decalage_sec)
        else:
            self._demarrer_conversion(
                installation_requise=False,
                chemin_ffprobe=chemin_ffprobe,
                decalage_sec=decalage_sec,
            )

    def _demarrer_conversion(self, installation_requise, decalage_sec, chemin_ffprobe=None):
        self.conversion_en_cours = True
        self._activer_ui(False)
        self.label_statut.config(text="Traitement en cours...")

        fichiers = list(self.fichiers_mp4)
        dossier_sortie = None if self.var_a_cote_source.get() else self.var_dossier_export.get()

        thread = threading.Thread(
            target=self._travail_conversion,
            args=(fichiers, dossier_sortie, installation_requise, chemin_ffprobe, decalage_sec),
            daemon=True,
        )
        thread.start()

    def _travail_conversion(
        self, fichiers, dossier_sortie, installation_requise, chemin_ffprobe, decalage_sec
    ):
        if installation_requise:
            reussite = core.telecharger_ffmpeg_portable(
                journaliser=self._journaliser_depuis_thread
            )
            if not reussite:
                self._journaliser_depuis_thread("Échec de l'installation de ffmpeg, arrêt.")
                self.file_attente.put(("fin", None))
                return
            chemin_ffprobe = core.trouver_ffprobe()
            if chemin_ffprobe is None:
                self._journaliser_depuis_thread(
                    "ERREUR : ffprobe reste introuvable après installation."
                )
                self.file_attente.put(("fin", None))
                return

        nb_reussis = 0
        for chemin_mp4 in fichiers:
            ok = core.traiter_fichier(
                chemin_ffprobe,
                chemin_mp4,
                dossier_sortie=dossier_sortie if dossier_sortie else None,
                journaliser=self._journaliser_depuis_thread,
                decalage_sec=decalage_sec,
            )
            if ok:
                nb_reussis += 1

        self._journaliser_depuis_thread(
            f"\nTerminé : {nb_reussis}/{len(fichiers)} fichier(s) traité(s) avec succès."
        )
        self.file_attente.put(("fin", None))

    def _fin_conversion(self):
        self.conversion_en_cours = False
        self._activer_ui(True)
        self.label_statut.config(text="Terminé.")


if __name__ == "__main__":
    # Une seule fenêtre à la fois : si une instance tourne déjà, on prévient
    # l'utilisateur et on quitte plutôt que d'en ouvrir une seconde.
    _verrou_instance_unique = _acquerir_verrou_instance_unique()
    if _verrou_instance_unique is None:
        racine_temporaire = tk.Tk()
        racine_temporaire.withdraw()
        messagebox.showwarning(
            "Déjà ouvert",
            "MarkerBridge est déjà ouvert dans une autre fenêtre.\n\n"
            "Une seule instance à la fois est autorisée pour éviter les conflits "
            "pendant une récupération ou une conversion.",
        )
        racine_temporaire.destroy()
        sys.exit(0)

    app = ApplicationMarqueurs()
    app.mainloop()
