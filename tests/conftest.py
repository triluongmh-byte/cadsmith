"""Shared shapes for the tests."""

import cadquery as cq
import pytest


def box(x, y, z, at=(0, 0, 0)):
    return cq.Workplane("XY").box(x, y, z).translate(at).val()


@pytest.fixture
def open_box():
    def make(wall, length=40, width=30, height=20):
        return cq.Workplane("XY").box(length, width, height).faces(">Z").shell(-wall).val()

    return make
