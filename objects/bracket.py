# object: Bracket plate
# blurb: Flat plate with two holes.
# Bracket plate for orcad — runnable build123d program (see header above).
from build123d import *

L = 60  # spec: number label=Length unit=mm min=10 max=300 step=0.5
W = 30  # spec: number label=Width unit=mm min=10 max=200 step=0.5
T = 5  # spec: number label=Thickness unit=mm min=1 max=50 step=0.5
D = 5  # spec: number label=Hole dia unit=mm min=1 max=50 step=0.5

plate = Box(L, W, T)
hole = Cylinder(D / 2, T + 2)
h1 = Pos(-L / 4, 0, -1) * hole
h2 = Pos(L / 4, 0, -1) * hole
result = plate - h1 - h2
