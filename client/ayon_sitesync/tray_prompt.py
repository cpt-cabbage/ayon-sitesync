"""One-time "studio or remote?" machine prompt shown in the tray.

Only imported from the tray process (Qt is available there); never import
this module from DCC or headless code paths.
"""
from qtpy import QtWidgets

from .machine_role import (
    ROLE_STUDIO,
    ROLE_REMOTE,
    save_machine_role,
    get_default_local_root_base,
)


def show_machine_role_prompt(addon):
    """Ask the artist where this machine works from and remember it.

    Answering is the single manual step of zero-touch site setup. "Ask me
    later" (or closing the dialog) keeps the reachability-probe fallback
    and re-asks at next tray start.
    """
    if QtWidgets.QApplication.instance() is None:
        return

    box = QtWidgets.QMessageBox()
    box.setWindowTitle("AYON Site Sync")
    box.setIcon(QtWidgets.QMessageBox.Question)
    box.setText("Where is this machine working from?")
    box.setInformativeText(
        "In the studio: files are used directly from the studio"
        " storage.\n\n"
        "Remote: you work in a local folder ({}) and published files"
        " sync with the studio in the background.\n\n"
        "This is remembered for this machine. To change it later, pick"
        " 'My Active Site' in your site settings on the AYON web"
        " page.".format(get_default_local_root_base())
    )
    studio_btn = box.addButton(
        "In the studio", QtWidgets.QMessageBox.AcceptRole
    )
    remote_btn = box.addButton(
        "Remote / from home", QtWidgets.QMessageBox.AcceptRole
    )
    box.addButton("Ask me later", QtWidgets.QMessageBox.RejectRole)
    box.setDefaultButton(studio_btn)
    box.exec_()

    clicked = box.clickedButton()
    if clicked is studio_btn:
        role = ROLE_STUDIO
    elif clicked is remote_btn:
        role = ROLE_REMOTE
    else:
        return

    try:
        save_machine_role(role)
    except Exception:
        addon.log.warning("Couldn't save machine role", exc_info=True)
        return

    # Apply immediately: drop cached roles/settings so the running sync
    # loop picks the answer up without a tray restart.
    addon._machine_role_by_project.clear()
    try:
        addon.set_sync_project_settings()
        addon.reset_timer()
    except Exception:
        addon.log.warning(
            "Couldn't refresh sync settings after role prompt",
            exc_info=True
        )
