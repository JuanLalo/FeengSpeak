#!/usr/bin/env python3
"""Tests de la normalización de texto a habla (normalize_for_speech).

Puro texto, sin modelo: corre rápido y aislado.
    venv/bin/python -m unittest test_normalize -v
"""
import unittest

from feengspeak import normalize_for_speech as norm


class TestIdentifiers(unittest.TestCase):
    def test_snake_case(self):
        self.assertEqual(norm("_stream_play_worker"), "stream play worker")

    def test_camel_case(self):
        self.assertEqual(norm("MessageDisplay"), "Message Display")

    def test_camel_with_acronym_prefix(self):
        # En inglés no se deletrea: solo se separa.
        self.assertEqual(norm("HTTPServer", "en-us"), "HTTP Server")

    def test_kebab_case(self):
        self.assertEqual(norm("auto-detect"), "auto detect")

    def test_dotted_module(self):
        self.assertEqual(norm("module.function"), "module function")


class TestPaths(unittest.TestCase):
    def test_home_config_path(self):
        out = norm("~/.config/feengspeak/config.json")
        self.assertEqual(out, "config feengspeak config yeson")

    def test_keeps_fraction(self):
        self.assertIn("1/2", norm("la mitad 1/2"))

    def test_dotfile(self):
        self.assertEqual(norm(".config"), "config")


class TestExtensions(unittest.TestCase):
    def test_py(self):
        self.assertEqual(norm("feengspeak.py"), "feengspeak python")

    def test_known_extensions(self):
        self.assertEqual(norm("setup.sh"), "setup shell")
        self.assertEqual(norm("notes.md"), "notes markdown")


class TestAcronyms(unittest.TestCase):
    def test_spelled_in_spanish(self):
        self.assertEqual(norm("API"), "a pe i")
        self.assertEqual(norm("SQL"), "ese cu ele")
        self.assertEqual(norm("URL"), "u erre ele")

    def test_word_acronyms_fixed(self):
        self.assertEqual(norm("JSON"), "yeson")
        self.assertEqual(norm("RAM"), "ram")

    def test_not_spelled_in_english(self):
        self.assertEqual(norm("API", "en-us"), "API")


class TestVersionsAndUnits(unittest.TestCase):
    def test_version(self):
        self.assertEqual(norm("v1.0"), "versión 1.0")

    def test_units(self):
        self.assertEqual(norm("340 MB"), "340 megabytes")
        self.assertEqual(norm("0.7s"), "0.7 segundos")
        self.assertEqual(norm("24000 Hz"), "24000 hertz")


class TestSymbols(unittest.TestCase):
    def test_arrows(self):
        self.assertEqual(norm("a -> b"), "a , b")
        self.assertEqual(norm("x => y"), "x , y")


class TestSpanishNotBroken(unittest.TestCase):
    def test_plain_prose_untouched(self):
        s = "Listo, ambos repos quedaron en GitHub con historial limpio."
        self.assertEqual(norm(s), s)

    def test_no_false_acronym(self):
        # Palabras normales no se tocan.
        self.assertEqual(norm("la voz"), "la voz")


if __name__ == "__main__":
    unittest.main()
