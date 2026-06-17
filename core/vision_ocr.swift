// vision_ocr.swift - RezeptCheck
// Lokale OCR über Apple Vision. Liest ein Bild und gibt erkannten Text zeilenweise aus.
// Läuft komplett auf dem Mac, keine Daten verlassen das Gerät (DSGVO-konform).
// Aufruf:  swift vision_ocr.swift <bildpfad>

import Foundation
import Vision
import AppKit

guard CommandLine.arguments.count > 1 else {
    FileHandle.standardError.write("Usage: vision_ocr <bild>\n".data(using: .utf8)!)
    exit(1)
}

let pfad = CommandLine.arguments[1]

guard let bild = NSImage(contentsOfFile: pfad),
      let tiff = bild.tiffRepresentation,
      let bitmap = NSBitmapImageRep(data: tiff),
      let cg = bitmap.cgImage else {
    FileHandle.standardError.write("Bild konnte nicht geladen werden: \(pfad)\n".data(using: .utf8)!)
    exit(1)
}

let request = VNRecognizeTextRequest { (req, err) in
    guard let beobachtungen = req.results as? [VNRecognizedTextObservation] else { return }
    // Nach vertikaler Position sortieren (oben → unten), damit die Zeilen-Reihenfolge stimmt
    let sortiert = beobachtungen.sorted { a, b in
        a.boundingBox.origin.y > b.boundingBox.origin.y
    }
    for o in sortiert {
        if let kandidat = o.topCandidates(1).first {
            print(kandidat.string)
        }
    }
}

request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
request.recognitionLanguages = ["de-DE"]

let handler = VNImageRequestHandler(cgImage: cg, options: [:])
do {
    try handler.perform([request])
} catch {
    FileHandle.standardError.write("Vision-Fehler: \(error)\n".data(using: .utf8)!)
    exit(1)
}
