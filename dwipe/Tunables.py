#!/usr/bin/env python3
""" Ini file for dwipe"""
import configparser
import os
from .Utils import Utils

class Tunables:
    """Manages dwipe configuration and provides hardware-tuning defaults."""
    singleton = None

    
    def __init__(self):
        assert not Tunables.singleton

        config_dir = Utils.get_config_dir()
        self.path = config_dir / 'tunables.ini'
        self.config = configparser.ConfigParser()

        # Load existing or create default
        if not os.path.exists(self.path):
            self._create_default()
        else:
            self.config.read(self.path)

    @classmethod
    def get_singleton(cls):
        """ Get the singleton Tunable object """
        if cls.singleton is None:
            cls.singleton = cls()
        return cls.singleton

    def _create_default(self):
        """Generates a default .ini file with recommended 'turkey' hardware timings."""
        self.config['SATA_TIMING'] = {
            'post_password_delay': '1.0',
            'post_unlock_delay': '1.5',
            'reappearance_timeout': '120',
            'poll_interval': '5.0'
        }

        with open(self.path, 'w', encoding='utf-8') as f:
            f.write("# dwipe Configuration - Tune these for 'grumpy' hardware\n")
            self.config.write(f)

        # Fix ownership when running with sudo
        Utils.fix_file_ownership(self.path)

    @property
    def post_password_delay(self):
        return self.config.getfloat('SATA_TIMING', 'post_password_delay', fallback=1.0)

    @property
    def post_unlock_delay(self):
        return self.config.getfloat('SATA_TIMING', 'post_unlock_delay', fallback=1.5)

    @property
    def reappearance_timeout(self):
        return self.config.getfloat('SATA_TIMING', 'reappearance_timeout', fallback=120.0)

    @property
    def poll_interval(self):
        return self.config.getfloat('SATA_TIMING', 'poll_interval', fallback=5.0)
