// using System;
// using System.IO;
// using Autodesk.Revit.DB;

// namespace IFCAgent.RevitBuilder.Export;

// // Thin wrapper around Document.Export(IFCExportOptions).  Revit's bundled
// // IFC exporter handles MVD / schema selection; we just translate the
// // project schema string ("IFC4" / "IFC2X3") into the corresponding option
// // values and let Revit do the rest.
// public static class IfcExporter
// {
//     public static void Export(Document doc, string outPath, string schema)
//     {
//         var opts = new IFCExportOptions
//         {
//             FileVersion = ResolveVersion(schema),
//             ExportBaseQuantities = true,
//             SpaceBoundaryLevel = 1,
//             WallAndColumnSplitting = false,
//         };
//         // 2nd level boundaries are nicer for room semantics but slow;
//         // bump if downstream tools demand it.

//         var dir = Path.GetDirectoryName(outPath)!;
//         var name = Path.GetFileName(outPath);
//         Directory.CreateDirectory(dir);
//         doc.Export(dir, name, opts);
//     }

//     private static IFCVersion ResolveVersion(string schema)
//     {
//         return (schema ?? "IFC4").ToUpperInvariant() switch
//         {
//             "IFC2X3" => IFCVersion.IFC2x3CV2,
//             "IFC4X3" => TryParse("IFC4x3", IFCVersion.IFC4),
//             _        => IFCVersion.IFC4,  // IFC4 / default
//         };
//     }

//     // IFCVersion.IFC4x3 only exists in Revit 2024+.  Use reflection so we
//     // don't fail to compile against older Revit API DLLs.
//     private static IFCVersion TryParse(string name, IFCVersion fallback)
//     {
//         try
//         {
//             if (Enum.TryParse<IFCVersion>(name, ignoreCase: true, out var v))
//                 return v;
//         }
//         catch { }
//         return fallback;
//     }
// }


using System;
using System.IO;
using Autodesk.Revit.DB;

namespace IFCAgent.RevitBuilder.Export;

// 包装 Document.Export(IFCExportOptions)。
// Revit 2024+ 的 IFC 导出器内部要改写文档来加 IFC 元数据，
// 所以必须在一个 open transaction 里调用。
// 我们自己起一个名为 "IFCAgent.Export IFC" 的 transaction 包住调用，
// 与上游 Pipeline 的建模 transaction 解耦。
public static class IfcExporter
{
    public static void Export(Document doc, string outPath, string schema)
    {
        var opts = new IFCExportOptions
        {
            FileVersion = ResolveVersion(schema),
            ExportBaseQuantities = true,
            SpaceBoundaryLevel = 1,
            WallAndColumnSplitting = false,
        };

        var dir = Path.GetDirectoryName(outPath)!;
        var name = Path.GetFileName(outPath);

        Directory.CreateDirectory(dir);

        using (var tx = new Transaction(doc, "IFCAgent.Export IFC"))
        {
            tx.Start();

            doc.Export(dir, name, opts);

            tx.Commit();
        }
    }

    private static IFCVersion ResolveVersion(string schema)
    {
        return (schema ?? "IFC4").ToUpperInvariant() switch
        {
            "IFC2X3" => IFCVersion.IFC2x3CV2,
            "IFC4X3" => TryParse("IFC4x3", IFCVersion.IFC4),
            _ => IFCVersion.IFC4,
        };
    }

    private static IFCVersion TryParse(string name, IFCVersion fallback)
    {
        try
        {
            if (Enum.TryParse<IFCVersion>(
                    name,
                    ignoreCase: true,
                    out var version))
            {
                return version;
            }
        }
        catch
        {
            // 某些 Revit 版本可能没有对应 IFCVersion 枚举，回退到 fallback。
        }

        return fallback;
    }
}