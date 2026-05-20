#!/usr/bin/env python3
"""
Generate SVG floor plan from IFC using ifcopenshell's built-in SVG serializer.
Python equivalent of: IfcConvert input.ifc output.svg
"""

import argparse
import ifcopenshell
import ifcopenshell.geom as gm


def ifc_to_svg(ifc_path: str, svg_path: str):
    model = ifcopenshell.open(ifc_path)
    
    # Geometry settings
    s = gm.settings()
    s.set(s.USE_WORLD_COORDS, True)
    
    # Serializer settings
    ss = gm.serializer_settings()
    
    # Create SVG serializer
    serializer = gm.serializers.svg(svg_path, s, ss)
    serializer.setFile(model)
    
    # Configure for floor plan style output
    serializer.setWithoutStoreys(False)
    serializer.setAutoElevation(False)
    serializer.setAutoSection(False)
    serializer.setPrintSpaceNames(True)
    serializer.setPrintSpaceAreas(True)
    serializer.setDrawDoorArcs(True)
    
    # Write all products
    for product in model.by_type("IfcProduct"):
        try:
            if product.Representation:
                shape = gm.create_shape(s, product)
                serializer.write(shape)
        except Exception:
            pass
    
    serializer.finalize()
    print(f"Saved SVG → {svg_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ifc", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    ifc_to_svg(args.ifc, args.out)


if __name__ == "__main__":
    main()
