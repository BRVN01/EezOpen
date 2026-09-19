#!/usr/bin/python3

"""
obs_control for Linux

Author: Bruno Dias da Silva
Description:
    Provides basic OBS Studio control through the WebSocket API.

    Supports starting, pausing and stopping recordings, muting or
    unmuting audio inputs, and switching between scenes.

Copyright (c) 2026 Bruno Dias da Silva

Licensed under the MIT License.
See LICENSE for details.
"""

# This script has a basic OBS Studios control.
#
# https://pypi.org/project/obsws-python/
#
# pip install obsws_python

from __future__ import annotations

import obsws_python as obs
import argparse

class obsControl:
    def __init__(self):
        
        # Inicia argparse:
        parser = argparse.ArgumentParser()
    
        parser.add_argument("--start", action="store_true", help="Inicia a gravação.")
        parser.add_argument("--pause", action="store_true", help="Pausa a gravação.")
        parser.add_argument("--stop", action="store_true", help="Para a gravação.")

        parser.add_argument("--audio", help="Usado para mutar e desmutar um audio.")

        parser.add_argument("--scene", help="Usado para trocar de cena.")

        # Args recebe todos os argumentos do programa:
        self.args = parser.parse_args()

        # Action recebe somente o argumento em 'command':
        self.action = self.args

        self.connectOBS()

    def connectOBS(self):
        self.control = obs.ReqClient(host='192.168.1.20', port=4444, password='abc123', timeout=3)

        if self.args.start:
            self.StartRecord()
        elif self.args.pause:
            self.TogglePauseRecord()
        elif self.args.stop:
            self.StopRecord()
        elif self.args.audio:
            self.ToggleMic()
        elif self.args.scene:
            self.ToggleScene()

    def StartRecord(self):
        self.control.start_record()

    def TogglePauseRecord(self):
        self.control.toggle_record_pause()

    def StopRecord(self):
        self.control.stop_record()

    def ToggleMic(self):
        self.control.toggle_input_mute(self.args.audio)

    def ToggleScene(self):
        self.control.set_current_program_scene(self.args.scene)


if __name__ == '__main__':
    a = obsControl()


# On Linux Ubuntu We can see all Custom Shortcuts using the command below:
## dconf dump /org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/

## After You have all Custom Shortcuts, You can back up '~/.config/dconf/user'.

