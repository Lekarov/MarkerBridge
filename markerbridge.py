"""
MarkerBridge — cœur métier + CLI.

Convertit les chapitres OBS (marqueurs natifs des MP4 OBS 30.2+) en un fichier
XML FCP7/xmeml importable dans Adobe Premiere Pro (Fichier > Importer).

Le XML généré ne référence aucun fichier vidéo réel : il embarque un clip
"Color Matte" invisible (généré, opacité 0%), calé sur la durée réelle de la
vidéo, portant les marqueurs de chapitres OBS aux bons timecodes. L'utilisateur
glisse ensuite ce clip factice sur une piste au-dessus de sa vraie vidéo déjà
montée dans son propre projet : les marqueurs suivent, sans jamais dupliquer la
vidéo ni l'audio (voir CLAUDE.md pour le détail des contraintes xmeml/Premiere).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import uuid
import zipfile
from fractions import Fraction
from xml.dom import minidom
from xml.etree import ElementTree as ET

# Nom du logiciel et éditeur, utilisés pour le dossier de données applicatif.
NOM_LOGICIEL = "MarkerBridge"
NOM_EDITEUR = "DoktorP3st"

# Version du logiciel (semver, gérée à la main au fil des évolutions).
VERSION = "0.1.2"

# Lien vers le profil GitHub de l'auteur, affiché dans la fenêtre Paramètres de la GUI.
URL_GITHUB_AUTEUR = "https://github.com/Lekarov"

# URL du build portable "essentials" de ffmpeg (Windows, statique, sans installeur).
URL_FFMPEG_PORTABLE = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"


def obtenir_dossier_donnees():
    """Dossier de données de l'application, indépendant de l'endroit où le
    script est installé : %APPDATA%/DoktorP3st/MarkerBridge sur Windows (même
    convention que les autres outils DoktorP3st : WaveRouter, ElTac, TAC_MP4...).

    Fonctionne sur n'importe quel PC/utilisateur Windows sans dépendre du
    chemin d'installation. Si %APPDATA% est absent (hors Windows), on retombe
    sur le dossier personnel de l'utilisateur.
    """
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    dossier = os.path.join(base, NOM_EDITEUR, NOM_LOGICIEL)
    os.makedirs(dossier, exist_ok=True)
    return dossier


# Dossier où est installée la version portable de ffmpeg si besoin (voir
# obtenir_dossier_donnees ci-dessus : commun à tous les postes de l'utilisateur).
DOSSIER_DONNEES = obtenir_dossier_donnees()
DOSSIER_FFMPEG_LOCAL = os.path.join(DOSSIER_DONNEES, "ffmpeg_portable")


def trouver_ffprobe():
    """Cherche ffprobe : d'abord la copie portable locale, sinon le PATH système.

    Retourne le chemin vers ffprobe.exe, ou None si introuvable.
    """
    # 1. Copie portable déjà installée par ce script (utilisable hors ligne).
    if os.path.isdir(DOSSIER_FFMPEG_LOCAL):
        for racine, _dossiers, fichiers in os.walk(DOSSIER_FFMPEG_LOCAL):
            if "ffprobe.exe" in fichiers:
                return os.path.join(racine, "ffprobe.exe")

    # 2. ffprobe déjà présent dans le PATH du système.
    chemin_path = shutil.which("ffprobe")
    if chemin_path:
        return chemin_path

    return None


def telecharger_ffmpeg_portable(journaliser=print):
    """Télécharge et installe la version portable de ffmpeg, sans interaction.

    Nécessite une connexion Internet (à faire une seule fois, sur une machine
    connectée). Installe ffmpeg dans un dossier local à côté du script, pour
    un usage 100% hors ligne ensuite. Utilisé aussi bien par la CLI que la GUI.
    """
    journaliser(f"Téléchargement de ffmpeg depuis {URL_FFMPEG_PORTABLE} ...")
    chemin_zip = os.path.join(DOSSIER_DONNEES, "_ffmpeg_temp.zip")
    try:
        urllib.request.urlretrieve(URL_FFMPEG_PORTABLE, chemin_zip)
        journaliser("Téléchargement terminé, extraction en cours...")
        with zipfile.ZipFile(chemin_zip, "r") as archive:
            archive.extractall(DOSSIER_FFMPEG_LOCAL)
        journaliser(f"ffmpeg installé dans : {DOSSIER_FFMPEG_LOCAL}")
        return True
    except Exception as erreur:
        journaliser(f"ERREUR pendant le téléchargement/l'installation : {erreur}")
        return False
    finally:
        if os.path.exists(chemin_zip):
            os.remove(chemin_zip)


def proposer_installation_ffmpeg():
    """Version ligne de commande : demande confirmation via input() puis installe."""
    reponse = input(
        "ffprobe/ffmpeg est introuvable sur ce système.\n"
        "Voulez-vous télécharger automatiquement une version portable "
        "(nécessite Internet, une seule fois) ? [o/N] : "
    ).strip().lower()

    if reponse not in ("o", "oui", "y", "yes"):
        print(
            "\nInstallation annulée. Vous pouvez installer ffmpeg manuellement :\n"
            "  1. Téléchargez le build 'essentials' ici : "
            f"{URL_FFMPEG_PORTABLE}\n"
            "  2. Décompressez l'archive.\n"
            f"  3. Copiez le dossier extrait dans : {DOSSIER_FFMPEG_LOCAL}\n"
            "     (de sorte que 'ffprobe.exe' se trouve dans un sous-dossier 'bin').\n"
        )
        return False

    return telecharger_ffmpeg_portable(journaliser=print)


def executer_ffprobe(chemin_ffprobe, arguments, chemin_fichier):
    """Lance ffprobe avec les arguments donnés et renvoie le JSON parsé.

    Lève une exception explicite en cas d'échec (fichier corrompu, etc.).
    """
    commande = [chemin_ffprobe, "-v", "error"] + arguments + [chemin_fichier]
    resultat = subprocess.run(
        commande, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )

    if resultat.returncode != 0:
        raise RuntimeError(
            f"ffprobe a échoué sur '{chemin_fichier}' : {resultat.stderr.strip()}"
        )

    try:
        return json.loads(resultat.stdout)
    except json.JSONDecodeError as erreur:
        raise RuntimeError(
            f"Réponse ffprobe invalide pour '{chemin_fichier}' : {erreur}"
        )


# Pas de décalage par défaut : les chapitres OBS sont déjà au timecode exact où
# l'utilisateur a appuyé sur le raccourci. L'écart parfois observé avec le "vrai"
# moment vient du temps de réaction humain (variable d'une prise à l'autre), pas d'un
# problème de synchro du fichier — un décalage fixe ne peut donc pas le corriger de
# façon fiable. Le réglage reste dispo (--decalage / champ GUI) pour un ajustement
# ponctuel au cas par cas, mais 0 est la valeur par défaut correcte.
DECALAGE_PAR_DEFAUT_SEC = 0.0


def extraire_chapitres(chemin_ffprobe, chemin_fichier, decalage_sec=0.0):
    """Retourne la liste des chapitres OBS : [{"debut_sec": float, "titre": str}, ...].

    decalage_sec est ajouté au début de chaque chapitre pour compenser la latence
    d'OBS entre l'appui sur le raccourci et le moment réellement encodé (les
    marqueurs générés arrivent sinon un peu trop tôt par rapport au son réel).
    """
    donnees = executer_ffprobe(
        chemin_ffprobe, ["-show_chapters", "-of", "json"], chemin_fichier
    )

    chapitres = []
    for chapitre in donnees.get("chapters", []):
        debut_sec = max(float(chapitre.get("start_time", 0)) + decalage_sec, 0.0)
        titre = chapitre.get("tags", {}).get("title", "").strip()
        chapitres.append({"debut_sec": debut_sec, "titre": titre})

    return chapitres


def extraire_infos_media(chemin_ffprobe, chemin_fichier):
    """Retourne framerate, résolution, durée totale et caractéristiques audio
    (si présent) : tout ce qu'il faut pour embarquer le clip d'origine dans le XML.
    """
    donnees = executer_ffprobe(
        chemin_ffprobe, ["-show_streams", "-show_format", "-of", "json"], chemin_fichier
    )

    flux = donnees.get("streams", [])
    flux_video = next((f for f in flux if f.get("codec_type") == "video"), None)
    if flux_video is None or "r_frame_rate" not in flux_video:
        raise RuntimeError(
            f"Impossible de déterminer les caractéristiques vidéo de '{chemin_fichier}' "
            "(aucun flux vidéo trouvé ?)."
        )

    fraction_brute = flux_video["r_frame_rate"]  # ex: "30000/1001" ou "30/1"
    fps_reel = float(Fraction(fraction_brute))
    if fps_reel <= 0:
        raise RuntimeError(f"Framerate invalide pour '{chemin_fichier}'.")

    # Détection d'une cadence variable (VFR) : la conversion secondes -> frames
    # suppose une cadence constante. Si avg_frame_rate s'écarte nettement de
    # r_frame_rate, les marqueurs dériveraient — on le signale à l'appelant
    # (traiter_fichier journalise un avertissement) plutôt que d'échouer,
    # car OBS produit normalement du CFR.
    cadence_variable = False
    try:
        fps_moyen = float(Fraction(flux_video.get("avg_frame_rate", fraction_brute)))
        if fps_moyen > 0:
            cadence_variable = abs(fps_moyen - fps_reel) / fps_reel > 0.001
    except (ValueError, ZeroDivisionError):
        pass

    timebase = round(fps_reel)
    # Les cadences NTSC (29.97, 59.94, 23.976...) ne sont pas des entiers exacts :
    # Premiere doit alors utiliser un timebase arrondi + le flag NTSC=TRUE.
    est_ntsc = abs(fps_reel - timebase) > 0.001

    largeur = int(flux_video.get("width", 0))
    hauteur = int(flux_video.get("height", 0))
    if not largeur or not hauteur:
        raise RuntimeError(f"Résolution vidéo introuvable pour '{chemin_fichier}'.")

    # Durée du clip matte : priorité au décompte de frames du flux vidéo
    # (nb_frames, exact, aucun arrondi), sinon la durée du flux vidéo.
    # format.duration en dernier recours seulement : c'est la durée du
    # conteneur entier, généralement calée sur l'audio qu'OBS laisse dépasser
    # la vidéo de quelques centaines de ms — le matte débordait d'autant.
    duree_frames = None
    try:
        nb_frames_brut = int(flux_video.get("nb_frames", 0))
        if nb_frames_brut > 0:
            duree_frames = nb_frames_brut
    except (TypeError, ValueError):
        pass

    duree_sec = 0.0
    for duree_brute in (
        flux_video.get("duration"),
        donnees.get("format", {}).get("duration"),
    ):
        try:
            duree_sec = float(duree_brute)
        except (TypeError, ValueError):
            continue
        if duree_sec > 0:
            break
    if duree_frames is None and duree_sec <= 0:
        raise RuntimeError(f"Durée vidéo introuvable pour '{chemin_fichier}'.")

    flux_audio = next((f for f in flux if f.get("codec_type") == "audio"), None)
    infos_audio = None
    if flux_audio is not None:
        infos_audio = {
            "echantillonnage": int(flux_audio.get("sample_rate", 48000)),
            "canaux": int(flux_audio.get("channels", 2)),
        }

    return {
        "fps_reel": fps_reel,
        "timebase": timebase,
        "est_ntsc": est_ntsc,
        "cadence_variable": cadence_variable,
        "largeur": largeur,
        "hauteur": hauteur,
        "duree_sec": duree_sec,
        "duree_frames": duree_frames,  # None si le conteneur ne fournit pas nb_frames
        "audio": infos_audio,
    }


def secondes_vers_frame(secondes, fps_reel):
    """Convertit un timecode en secondes vers un numéro de frame (arrondi)."""
    return round(secondes * fps_reel)


def formater_timecode(secondes):
    """Formate un nombre de secondes en HH:MM:SS pour affichage lisible."""
    total_secondes = int(round(secondes))
    heures, reste = divmod(total_secondes, 3600)
    minutes, secs = divmod(reste, 60)
    return f"{heures:02d}:{minutes:02d}:{secs:02d}"


def nommer_marqueur(titre_obs, index, debut_sec):
    """Donne un nom clair au marqueur.

    OBS nomme parfois ses chapitres par défaut avec de simples numéros
    ("1", "2", ...) plutôt qu'un vrai titre : ce n'est pas plus parlant qu'un
    titre vide. Dans ce cas (ou si le titre est vide), on génère un nom
    explicite basé sur le numéro d'ordre et le timecode du marqueur.
    """
    titre_nettoye = titre_obs.strip()
    if titre_nettoye and not titre_nettoye.isdigit():
        return titre_nettoye
    return f"Marqueur {index} - {formater_timecode(debut_sec)}"


def construire_xml_marqueurs(nom_sequence, chapitres, infos_media):
    """Construit l'arbre XML FCP7/xmeml : un clip "Color Matte" invisible (opacité 0,
    généré, sans référencer aucun fichier réel) calé sur la durée de la vidéo, portant
    les marqueurs de chapitres OBS. L'utilisateur importe ce XML et glisse juste ce
    clip factice sur une piste au-dessus de sa vraie vidéo déjà montée : les marqueurs
    suivent, sans dupliquer la vidéo ni l'audio (contrairement à embarquer le vrai
    fichier, qui posait des problèmes d'audio en multi-pistes OBS).

    Structure et ordre validés en conditions réelles (fichier généré par un outil de
    conversion de marqueurs tiers largement utilisé, confirmé fonctionnel) :
    - <marker> : comment, name, in, out — PAS name/in/out/comment comme le laisse
      penser la doc DTD Apple publique. Premiere lit ça de façon positionnelle et
      ignore silencieusement les marqueurs si l'ordre est différent.
    - Le clip porteur est un <generatoritem> (générateur "Color" + filtre opacité à
      0), pas un <clipitem> référençant un fichier.
    """
    fps_reel = infos_media["fps_reel"]
    timebase = infos_media["timebase"]
    est_ntsc = infos_media["est_ntsc"]
    # nb_frames du flux vidéo quand disponible (décompte exact, aucun arrondi),
    # sinon conversion depuis la durée du flux vidéo en secondes.
    duree_totale_frames = infos_media.get("duree_frames") or max(
        secondes_vers_frame(infos_media["duree_sec"], fps_reel), 1
    )

    def ajouter_bloc_rate(parent):
        rate = ET.SubElement(parent, "rate")
        ET.SubElement(rate, "timebase").text = str(timebase)
        ET.SubElement(rate, "ntsc").text = "TRUE" if est_ntsc else "FALSE"
        return rate

    def ajouter_marqueur(parent, nom, frame_debut):
        marqueur = ET.SubElement(parent, "marker")
        ET.SubElement(marqueur, "comment").text = ""
        ET.SubElement(marqueur, "name").text = nom
        ET.SubElement(marqueur, "in").text = str(frame_debut)
        ET.SubElement(marqueur, "out").text = "-1"  # marqueur ponctuel, pas une plage

    marqueurs_a_poser = [
        (nommer_marqueur(chapitre["titre"], index, chapitre["debut_sec"]),
         secondes_vers_frame(chapitre["debut_sec"], fps_reel))
        for index, chapitre in enumerate(chapitres, start=1)
    ]

    racine = ET.Element("xmeml", version="4")
    sequence = ET.SubElement(racine, "sequence", id="sequence")
    ET.SubElement(sequence, "uuid").text = uuid.uuid4().hex
    ET.SubElement(sequence, "duration").text = str(duree_totale_frames)
    ajouter_bloc_rate(sequence)
    ET.SubElement(sequence, "name").text = nom_sequence

    media = ET.SubElement(sequence, "media")
    video = ET.SubElement(media, "video")
    format_video = ET.SubElement(video, "format")
    caract_video = ET.SubElement(format_video, "samplecharacteristics")
    ajouter_bloc_rate(caract_video)
    ET.SubElement(caract_video, "width").text = str(infos_media["largeur"])
    ET.SubElement(caract_video, "height").text = str(infos_media["hauteur"])
    ET.SubElement(caract_video, "anamorphic").text = "FALSE"
    ET.SubElement(caract_video, "pixelaspectratio").text = "square"
    ET.SubElement(caract_video, "fielddominance").text = "none"

    piste_video = ET.SubElement(video, "track")
    ET.SubElement(piste_video, "enabled").text = "TRUE"
    ET.SubElement(piste_video, "locked").text = "FALSE"

    matte = ET.SubElement(piste_video, "generatoritem", id="clipitem-1")
    ET.SubElement(matte, "name").text = f"Marqueurs {nom_sequence}"
    ET.SubElement(matte, "enabled").text = "TRUE"
    ET.SubElement(matte, "duration").text = str(duree_totale_frames)
    ajouter_bloc_rate(matte)
    ET.SubElement(matte, "start").text = "0"
    ET.SubElement(matte, "end").text = str(duree_totale_frames)
    ET.SubElement(matte, "in").text = "0"
    ET.SubElement(matte, "out").text = str(duree_totale_frames)
    ET.SubElement(matte, "alphatype").text = "none"

    effet = ET.SubElement(matte, "effect")
    ET.SubElement(effet, "name").text = "Color"
    ET.SubElement(effet, "effectid").text = "Color"
    ET.SubElement(effet, "effectcategory").text = "Matte"
    ET.SubElement(effet, "effecttype").text = "generator"
    ET.SubElement(effet, "mediatype").text = "video"
    parametre_couleur = ET.SubElement(effet, "parameter", authoringApp="PremierePro")
    ET.SubElement(parametre_couleur, "parameterid").text = "fillcolor"
    ET.SubElement(parametre_couleur, "name").text = "Color"
    valeur_couleur = ET.SubElement(parametre_couleur, "value")
    ET.SubElement(valeur_couleur, "alpha").text = "0"
    ET.SubElement(valeur_couleur, "red").text = "0"
    ET.SubElement(valeur_couleur, "green").text = "0"
    ET.SubElement(valeur_couleur, "blue").text = "0"

    filtre = ET.SubElement(matte, "filter")
    effet_opacite = ET.SubElement(filtre, "effect")
    ET.SubElement(effet_opacite, "name").text = "Opacity"
    ET.SubElement(effet_opacite, "effectid").text = "opacity"
    ET.SubElement(effet_opacite, "effectcategory").text = "motion"
    ET.SubElement(effet_opacite, "effecttype").text = "motion"
    ET.SubElement(effet_opacite, "mediatype").text = "video"
    parametre_opacite = ET.SubElement(effet_opacite, "parameter", authoringApp="PremierePro")
    ET.SubElement(parametre_opacite, "parameterid").text = "opacity"
    ET.SubElement(parametre_opacite, "name").text = "opacity"
    ET.SubElement(parametre_opacite, "valuemin").text = "0"
    ET.SubElement(parametre_opacite, "valuemax").text = "100"
    ET.SubElement(parametre_opacite, "value").text = "0"

    # Marqueurs posés sur le clip généré (visibles au survol dans le bin/source)...
    for nom, frame_debut in marqueurs_a_poser:
        ajouter_marqueur(matte, nom, frame_debut)

    timecode = ET.SubElement(sequence, "timecode")
    ajouter_bloc_rate(timecode)
    ET.SubElement(timecode, "string").text = "00:00:00:00"
    ET.SubElement(timecode, "frame").text = "0"
    ET.SubElement(timecode, "displayformat").text = "NDF"

    # ...et sur la séquence elle-même, pour qu'ils apparaissent sur la règle de la
    # timeline une fois le clip glissé dans le montage.
    for nom, frame_debut in marqueurs_a_poser:
        ajouter_marqueur(sequence, nom, frame_debut)

    return racine


def ecrire_xml(racine_xml, chemin_sortie):
    """Sérialise l'arbre XML avec indentation lisible et déclaration DOCTYPE xmeml."""
    xml_brut = ET.tostring(racine_xml, encoding="utf-8")
    xml_indente = minidom.parseString(xml_brut).toprettyxml(indent="    ")

    # minidom ajoute sa propre déclaration XML ; on la remplace pour insérer
    # le DOCTYPE xmeml attendu par Premiere.
    lignes = xml_indente.splitlines()
    lignes[0] = '<?xml version="1.0" encoding="UTF-8"?>'
    lignes.insert(1, "<!DOCTYPE xmeml>")
    # Retire les lignes vides que minidom ajoute parfois entre les balises.
    contenu_final = "\n".join(ligne for ligne in lignes if ligne.strip())

    with open(chemin_sortie, "w", encoding="utf-8") as fichier:
        fichier.write(contenu_final + "\n")


def traiter_fichier(
    chemin_ffprobe,
    chemin_mp4,
    dossier_sortie=None,
    journaliser=print,
    decalage_sec=DECALAGE_PAR_DEFAUT_SEC,
):
    """Traite un fichier MP4 : extrait chapitres + framerate, génère le XML.

    Si dossier_sortie est None, le XML est écrit à côté du fichier source.
    journaliser permet de rediriger les messages (CLI : print, GUI : log widget).
    decalage_sec compense la latence OBS entre l'appui sur le raccourci de chapitre
    et le moment réellement encodé (voir DECALAGE_PAR_DEFAUT_SEC).
    """
    nom_base = os.path.splitext(os.path.basename(chemin_mp4))[0]
    journaliser(f"\n--- Traitement de : {chemin_mp4} ---")

    if not os.path.isfile(chemin_mp4):
        journaliser(
            "  ERREUR : ce fichier est introuvable (déplacé, renommé ou supprimé "
            "depuis sa sélection ?)."
        )
        return False

    try:
        chapitres = extraire_chapitres(chemin_ffprobe, chemin_mp4, decalage_sec=decalage_sec)
    except RuntimeError as erreur:
        journaliser(f"  ERREUR : {erreur}")
        return False

    if not chapitres:
        journaliser("  Aucun chapitre/marqueur trouvé dans ce fichier, il est ignoré.")
        return False

    try:
        infos_media = extraire_infos_media(chemin_ffprobe, chemin_mp4)
    except RuntimeError as erreur:
        journaliser(f"  ERREUR : {erreur}")
        return False

    journaliser(
        f"  {len(chapitres)} marqueur(s) trouvé(s), framerate réel : {infos_media['fps_reel']:.3f} fps "
        f"(timebase {infos_media['timebase']}, NTSC={'oui' if infos_media['est_ntsc'] else 'non'}), "
        f"{infos_media['largeur']}x{infos_media['hauteur']}, "
        f"audio={'oui' if infos_media['audio'] else 'non'}, décalage appliqué : "
        f"{decalage_sec:+.2f}s"
    )
    if infos_media["cadence_variable"]:
        journaliser(
            "  ATTENTION : cadence d'images variable détectée (VFR). Le placement des "
            "marqueurs suppose une cadence constante : ils peuvent dériver. Vérifiez "
            "les réglages d'enregistrement OBS (CFR recommandé)."
        )

    racine_xml = construire_xml_marqueurs(nom_base, chapitres, infos_media)

    dossier_cible = dossier_sortie if dossier_sortie else (os.path.dirname(chemin_mp4) or ".")
    os.makedirs(dossier_cible, exist_ok=True)
    chemin_sortie = os.path.join(dossier_cible, f"{nom_base}_marqueurs.xml")
    ecrire_xml(racine_xml, chemin_sortie)

    journaliser(f"  XML généré : {chemin_sortie}")
    return True


def lister_fichiers_mp4(chemin_entree):
    """Retourne la liste des .mp4 à traiter, que l'entrée soit un fichier ou un dossier."""
    if os.path.isfile(chemin_entree):
        if not chemin_entree.lower().endswith(".mp4"):
            raise ValueError(f"'{chemin_entree}' n'est pas un fichier .mp4")
        return [chemin_entree]

    if os.path.isdir(chemin_entree):
        fichiers = [
            os.path.join(chemin_entree, nom)
            for nom in sorted(os.listdir(chemin_entree))
            if nom.lower().endswith(".mp4")
        ]
        if not fichiers:
            raise ValueError(f"Aucun fichier .mp4 trouvé dans le dossier '{chemin_entree}'")
        return fichiers

    raise ValueError(f"Chemin introuvable : '{chemin_entree}'")


def main():
    parseur = argparse.ArgumentParser(
        description="Convertit les chapitres OBS d'un MP4 en marqueurs Premiere (XML FCP7)."
    )
    parseur.add_argument(
        "chemin", help="Fichier .mp4 unique, ou dossier contenant plusieurs .mp4"
    )
    parseur.add_argument(
        "--decalage",
        type=float,
        default=DECALAGE_PAR_DEFAUT_SEC,
        help=(
            "Décalage en secondes ajouté à chaque marqueur pour compenser la latence "
            f"OBS (défaut : {DECALAGE_PAR_DEFAUT_SEC}s). Mettre 0 pour désactiver."
        ),
    )
    arguments = parseur.parse_args()

    chemin_ffprobe = trouver_ffprobe()
    if chemin_ffprobe is None:
        if not proposer_installation_ffmpeg():
            sys.exit(1)
        chemin_ffprobe = trouver_ffprobe()
        if chemin_ffprobe is None:
            print("ERREUR : ffprobe reste introuvable après installation.")
            sys.exit(1)

    print(f"ffprobe utilisé : {chemin_ffprobe}")

    try:
        fichiers_mp4 = lister_fichiers_mp4(arguments.chemin)
    except ValueError as erreur:
        print(f"ERREUR : {erreur}")
        sys.exit(1)

    nb_reussis = 0
    for chemin_mp4 in fichiers_mp4:
        if traiter_fichier(chemin_ffprobe, chemin_mp4, decalage_sec=arguments.decalage):
            nb_reussis += 1

    print(f"\nTerminé : {nb_reussis}/{len(fichiers_mp4)} fichier(s) traité(s) avec succès.")


if __name__ == "__main__":
    main()
