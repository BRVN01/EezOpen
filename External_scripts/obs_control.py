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

from __future__ import annotations

import argparse
from pathlib import Path

import obsws_python as obs


class obsControl:
    def __init__(self):

        parser = argparse.ArgumentParser()

        parser.add_argument(
            "--config",
            required=True,
            help="Arquivo de configuração do OBS."
        )

        parser.add_argument(
            "--start",
            action="store_true",
            help="Inicia a gravação."
        )

        parser.add_argument(
            "--pause",
            action="store_true",
            help="Pausa a gravação."
        )

        parser.add_argument(
            "--stop",
            action="store_true",
            help="Para a gravação."
        )

        parser.add_argument(
            "--audio",
            help="Usado para mutar e desmutar um áudio."
        )

        parser.add_argument(
            "--scene",
            help="Usado para trocar de cena."
        )

        self.args = parser.parse_args()

        self.config = self.loadConfig(self.args.config)

        self.connectOBS()

    def loadConfig(self, config_file):
        """
        Lê somente as opções necessárias para conexão com OBS:

        obs-host
        obs-port
        obs-password

        Todas as outras opções são ignoradas.
        """

        config_path = Path(config_file).expanduser()

        if not config_path.is_file():
            raise SystemExit(
                f"Arquivo de configuração não encontrado: {config_path}"
            )

        config = {}

        valid_options = {
            "obs-host",
            "obs-port",
            "obs-password",
        }

        try:
            with config_path.open("r", encoding="utf-8") as file:
                for line in file:

                    line = line.strip()

                    # Ignora linhas vazias e comentários.
                    if not line or line.startswith("#"):
                        continue

                    # Ignora linhas que não tenham "=".
                    if "=" not in line:
                        continue

                    key, value = line.split("=", 1)

                    key = key.strip()
                    value = value.strip()

                    # Ignora qualquer opção que não seja necessária.
                    if key not in valid_options:
                        continue

                    config[key] = value

        except OSError as error:
            raise SystemExit(
                f"Erro ao ler arquivo de configuração: {error}"
            )

        required_options = {
            "obs-host",
            "obs-port",
            "obs-password",
        }

        missing = required_options - config.keys()

        if missing:
            raise SystemExit(
                "Opções ausentes no arquivo de configuração: "
                + ", ".join(sorted(missing))
            )

        try:
            config["obs-port"] = int(config["obs-port"])
        except ValueError:
            raise SystemExit(
                "A opção obs-port precisa ser um número inteiro."
            )

        return config

    def connectOBS(self):

        self.control = obs.ReqClient(
            host=self.config["obs-host"],
            port=self.config["obs-port"],
            password=self.config["obs-password"],
            timeout=3,
        )

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


if __name__ == "__main__":
    obsControl()


# On Linux Ubuntu We can see all Custom Shortcuts using the command below:
# dconf dump /org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/
#
# After You have all Custom Shortcuts, You can back up '~/.config/dconf/user'.
