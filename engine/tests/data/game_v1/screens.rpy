screen main_menu():
    vbox:
        textbutton _("Start") action Start()
        textbutton "Quit" action Quit(confirm=False)
        text "Version [config.version]"

init python:
    def notify_saved():
        renpy.notify(_("Game saved."))
