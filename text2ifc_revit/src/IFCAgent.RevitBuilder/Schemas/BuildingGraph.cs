using System.Collections.Generic;
using System.Text.Json.Serialization;

namespace IFCAgent.RevitBuilder.Schemas;

// POCOs that mirror ifc_agent/text2ifc/schemas.py's BuildingGraph.
// Linear dimensions are in MILLIMETRES; angles in DEGREES.
// JSON property names are snake_case (the Python side dumps with asdict()).

public sealed class BuildingGraphDto
{
    [JsonPropertyName("metadata")] public BuildingMetadataDto Metadata { get; set; } = new();
    [JsonPropertyName("storeys")] public List<StoreyDto> Storeys { get; set; } = new();
}

public sealed class BuildingMetadataDto
{
    [JsonPropertyName("name")] public string Name { get; set; } = "Generated Building";
    [JsonPropertyName("description")] public string Description { get; set; } = "";
    [JsonPropertyName("project_name")] public string ProjectName { get; set; } = "Text2IFC Project";
    [JsonPropertyName("site_name")] public string SiteName { get; set; } = "Default Site";
    [JsonPropertyName("schema")] public string Schema { get; set; } = "IFC4";
    [JsonPropertyName("length_unit")] public string LengthUnit { get; set; } = "MILLIMETRE";
}

public sealed class StoreyDto
{
    [JsonPropertyName("id")] public string Id { get; set; } = "";
    [JsonPropertyName("name")] public string Name { get; set; } = "";
    [JsonPropertyName("elevation")] public double Elevation { get; set; }
    [JsonPropertyName("height")] public double Height { get; set; } = 3000.0;

    [JsonPropertyName("walls")] public List<WallDto> Walls { get; set; } = new();
    [JsonPropertyName("openings")] public List<OpeningDto> Openings { get; set; } = new();
    [JsonPropertyName("columns")] public List<ColumnDto> Columns { get; set; } = new();
    [JsonPropertyName("slabs")] public List<SlabDto> Slabs { get; set; } = new();
    [JsonPropertyName("roofs")] public List<RoofDto> Roofs { get; set; } = new();
    [JsonPropertyName("railings")] public List<RailingDto> Railings { get; set; } = new();
    [JsonPropertyName("spaces")] public List<SpaceDto> Spaces { get; set; } = new();
    [JsonPropertyName("furniture")] public List<FurnitureDto> Furniture { get; set; } = new();
}

public sealed class WallDto
{
    [JsonPropertyName("id")] public string Id { get; set; } = "";
    [JsonPropertyName("start")] public double[] Start { get; set; } = new double[2];
    [JsonPropertyName("end")] public double[] End { get; set; } = new double[2];
    [JsonPropertyName("thickness")] public double Thickness { get; set; } = 200.0;
    [JsonPropertyName("height")] public double Height { get; set; } = 3000.0;
    [JsonPropertyName("material")] public string Material { get; set; } = "Concrete";
    [JsonPropertyName("is_external")] public bool IsExternal { get; set; } = true;
}

public sealed class OpeningDto
{
    [JsonPropertyName("id")] public string Id { get; set; } = "";
    [JsonPropertyName("host_wall")] public string HostWall { get; set; } = "";
    [JsonPropertyName("kind")] public string Kind { get; set; } = "door"; // door | window
    [JsonPropertyName("offset")] public double Offset { get; set; }
    [JsonPropertyName("width")] public double Width { get; set; } = 900.0;
    [JsonPropertyName("height")] public double Height { get; set; } = 2100.0;
    [JsonPropertyName("sill_height")] public double SillHeight { get; set; }
}

public sealed class ColumnDto
{
    [JsonPropertyName("id")] public string Id { get; set; } = "";
    [JsonPropertyName("position")] public double[] Position { get; set; } = new double[2];
    [JsonPropertyName("section")] public double[] Section { get; set; } = new double[] { 400.0, 400.0 };
    [JsonPropertyName("height")] public double Height { get; set; } = 3000.0;
    [JsonPropertyName("material")] public string Material { get; set; } = "Concrete";
}

public sealed class SlabDto
{
    [JsonPropertyName("id")] public string Id { get; set; } = "";
    [JsonPropertyName("boundary")] public List<double[]> Boundary { get; set; } = new();
    [JsonPropertyName("thickness")] public double Thickness { get; set; } = 200.0;
    [JsonPropertyName("elevation")] public double Elevation { get; set; }
    [JsonPropertyName("material")] public string Material { get; set; } = "Concrete";
    [JsonPropertyName("predefined_type")] public string PredefinedType { get; set; } = "FLOOR";
}

public sealed class RoofDto
{
    [JsonPropertyName("id")] public string Id { get; set; } = "";
    [JsonPropertyName("boundary")] public List<double[]> Boundary { get; set; } = new();
    [JsonPropertyName("thickness")] public double Thickness { get; set; } = 200.0;
    [JsonPropertyName("elevation")] public double Elevation { get; set; } = 3000.0;
    [JsonPropertyName("material")] public string Material { get; set; } = "Concrete";
    [JsonPropertyName("pitch_deg")] public double PitchDeg { get; set; }
}

public sealed class RailingDto
{
    [JsonPropertyName("id")] public string Id { get; set; } = "";
    [JsonPropertyName("polyline")] public List<double[]> Polyline { get; set; } = new();
    [JsonPropertyName("height")] public double Height { get; set; } = 1100.0;
    [JsonPropertyName("elevation")] public double Elevation { get; set; }
    [JsonPropertyName("material")] public string Material { get; set; } = "Steel";
}

public sealed class SpaceDto
{
    [JsonPropertyName("id")] public string Id { get; set; } = "";
    [JsonPropertyName("name")] public string Name { get; set; } = "";
    [JsonPropertyName("function")] public string Function { get; set; } = "";
    [JsonPropertyName("boundary")] public List<double[]> Boundary { get; set; } = new();
    [JsonPropertyName("elevation")] public double Elevation { get; set; }
    [JsonPropertyName("height")] public double Height { get; set; } = 3000.0;
    [JsonPropertyName("furniture")] public List<FurnitureDto> Furniture { get; set; } = new();
    [JsonPropertyName("door_side")] public string DoorSide { get; set; } = "";
    [JsonPropertyName("window_side")] public string WindowSide { get; set; } = "";
}

public sealed class FurnitureDto
{
    [JsonPropertyName("id")] public string Id { get; set; } = "";
    [JsonPropertyName("ifc_class")] public string IfcClass { get; set; } = "IfcFurniture";
    [JsonPropertyName("predefined_type")] public string PredefinedType { get; set; } = "NOTDEFINED";
    [JsonPropertyName("name")] public string Name { get; set; } = "";
    [JsonPropertyName("position")] public double[] Position { get; set; } = new double[2];
    [JsonPropertyName("size")] public double[] Size { get; set; } = new double[] { 600.0, 600.0, 750.0 };
    [JsonPropertyName("rot_z_deg")] public double RotZDeg { get; set; }
    [JsonPropertyName("elevation")] public double Elevation { get; set; }
    [JsonPropertyName("material")] public string Material { get; set; } = "Wood";
}
