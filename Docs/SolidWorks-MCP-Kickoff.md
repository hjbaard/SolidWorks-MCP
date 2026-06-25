# SolidWorks MCP Connector — Kickoff Brief

> Plak dit in een nieuwe Claude Code-sessie als startcontext. Het beschrijft *wat* we bouwen, *hoe* de architectuur eruitziet, een concrete eerste set tools, de verificatie-loop, en de bekende valkuilen — zodat je niet vanaf nul de SolidWorks-API hoeft te ontdekken.

## 1. Doel

Bouw een MCP-server (Python) die een **lokaal draaiende SolidWorks-instantie** aanstuurt via de COM-API, zodat een AI-agent parametrische modellen kan opbouwen, meten en exporteren. De kern van het project is niet alleen "geometrie maken", maar een **closed loop**: bouwen → meten/verifiëren → vergelijken met requirements → corrigeren → herhalen.

### Waarom dit kansrijk is
Parametrische CAD levert *harde, verifieerbare signalen* (rebuild errors, mass properties, measure, interference checks). Dat maakt een agentische correctie-loop realistisch — anders dan bij puur visueel mesh-werk waar de enige feedback "het ziet er ongeveer goed uit" is.

### Scope v0 (bewust klein)
Eén onderdeel (part) kunnen opbouwen met de meest voorkomende features, en jezelf kunnen verifiëren. Assemblies, drawings en simulatie komen later.

## 2. Technische stack

- **Taal:** Python 3.11+
- **SolidWorks-koppeling:** `pywin32` (`win32com.client`) — COM/OLE Automation
- **MCP:** officiële Python MCP SDK (`mcp` package), stdio transport
- **Omgeving:** Windows, met SolidWorks geïnstalleerd en een actieve licentie. Geen cloud — alles lokaal.

### Verbinding maken met SolidWorks
```python
import win32com.client
import pythoncom

# Pak een draaiende instance, of start er een
try:
    swApp = win32com.client.GetActiveObject("SldWorks.Application")
except pythoncom.com_error:
    swApp = win32com.client.Dispatch("SldWorks.Application")

swApp.Visible = True  # tijdens ontwikkeling: zien wat er gebeurt
```

> Tip: gebruik `win32com.client.gencache.EnsureDispatch("SldWorks.Application")` voor early-binding en betere autocompletion van constanten. De `swconst` enums (bv. `swDocPART`, `swSketchManager` opties) zitten in de typelibrary.

## 3. Architectuurprincipes

1. **Eén COM-sessie, single-threaded.** De SolidWorks-API is stateful en niet thread-safe. Serialiseer alle calls. MCP stdio is sowieso sequentieel — houd het zo.
2. **Defensief met state.** Selectie, actieve document en context bepalen of een operatie slaagt. Neem nooit aan dat de juiste doc/selectie actief is — zet die expliciet in elke tool.
3. **Idempotent waar mogelijk + naamgeving.** Geef features en sketches expliciete namen (`feature.Name = "..."`) zodat latere tools ze betrouwbaar terugvinden i.p.v. via selectie-volgorde.
4. **Elke mutatie retourneert verificatie.** Een tool die iets bouwt, geeft ook terug: rebuild-status, foutmeldingen, en (waar relevant) de gewijzigde mass properties. De loop leeft van die feedback.
5. **Faal luid en gestructureerd.** Vang COM-errors, geef leesbare boodschappen terug i.p.v. stack traces. Een mislukte rebuild is geen crash maar een resultaat dat de agent moet kunnen lezen.

## 4. Eerste set tools (v0)

| Tool | Doel | Belangrijkste API-aanknopingspunten |
|---|---|---|
| `connect` / `get_status` | Verbind met/inspecteer SolidWorks; rapporteer actieve doc | `GetActiveObject`, `swApp.ActiveDoc` |
| `new_part` | Maak een nieuw leeg part-document | `swApp.NewDocument` (part template path) |
| `open_part` / `save_part` | Open/sla op | `swApp.OpenDoc6`, `model.SaveAs3` |
| `create_sketch` | Start sketch op een gekozen vlak (Front/Top/Right of face) | `model.SketchManager`, `SelectByID2` op het vlak |
| `add_extrude` | Extrude de actieve/benoemde sketch | `FeatureManager.FeatureExtrusion3` |
| `add_revolve` | Revolve rond as | `FeatureManager.FeatureRevolve2` |
| `add_fillet` | Fillet op geselecteerde edges | `FeatureManager.FeatureFillet3` |
| `set_dimension` | Wijzig een benoemde maat (parametrisch!) | `model.Parameter("D1@Sketch1").SystemValue` |
| `set_equation` | Voeg/zet equation toe | `model.GetEquationMgr` |
| `rebuild` | Forceer rebuild en lees fouten | `model.ForceRebuild3`, `model.GetFeatureCount` + error-check |
| `get_mass_properties` | Volume, massa, zwaartepunt, bounding box | `model.Extension.CreateMassProperty` |
| `measure` | Afstand/hoek tussen selecties | `model.Extension.CreateMeasure` |
| `check_interference` (later) | Botsingen in assembly | `InterferenceDetectionMgr` |
| `screenshot` | Render viewport naar PNG voor visuele check | `model.SaveAs3` naar `.png`, of `model.ActiveView` capture |
| `export` | Exporteer naar STEP/STL | `model.SaveAs3` met juiste extensie/opties |

> Eenheden: SolidWorks API werkt intern in **meters** (en radialen voor hoeken), ongeacht de document-units. Reken expliciet om (bv. mm → m) en documenteer dat in elke tool, dit is een klassieke bug-bron.

## 5. De verificatie-loop (het hart van het project)

Na elke bouwstap moet de agent kunnen vaststellen of het resultaat klopt. Bouw de loop rond deze signalen:

1. **Rebuild-status** — faalde een feature? Is een sketch onderbepaald (dangling)?
2. **Mass properties** — vergelijk volume/massa/zwaartepunt met de verwachte spec.
3. **Measure** — verifieer kritische maten, wanddiktes, afstanden tegen de requirements.
4. **Bounding box** — past het binnen het beoogde gabarit / printbed?
5. **Visuele check** — screenshot teruglezen voor grove fouten (verkeerde oriëntatie, ontbrekende feature).
6. **(Later) Interference/FEA** — assembly-botsingen of sterkte.

Een typische iteratie:
```
intentie + requirements
  → bouw feature via API
  → rebuild + lees fouten
  → meet (mass props / measure / bbox)
  → vergelijk met spec
  → wijk af? → set_dimension / pas feature aan → herhaal
  → akkoord? → export STEP/STL
```

## 6. Bekende valkuilen (vooraf weten = tijd besparen)

- **COM is stateful & single-threaded.** Selecties en actieve doc bepalen succes. Zet ze expliciet; reset selectie met `model.ClearSelection2(True)` tussen operaties.
- **Eenheden:** API in meters/radialen, niet in de document-units. Veelvoorkomende stille fout.
- **`SelectByID2` is fragiel.** Selecteren op naam/coördinaat is gevoelig. Benoem geometrie en gebruik waar mogelijk de FeatureManager-tree i.p.v. ruimtelijke selectie.
- **Booleans op return-waarden.** Veel API-calls geven `False`/`None` bij falen zonder exception. Controleer return-waarden actief.
- **Templates vereisen een pad.** `NewDocument`/`NewPart` heeft een geldig part-template nodig; haal het standaardpad op via `swApp.GetUserPreferenceStringValue(swDefaultTemplatePart)`.
- **Versie-afhankelijke API-namen.** Veel functies hebben genummerde varianten (`...3`, `...6`). Gebruik de nieuwste die jouw SolidWorks-versie ondersteunt.
- **Design intent ≠ geldige geometrie.** Een AI maakt makkelijk *geldige* geometrie; een feature-tree die *robuust mee-update* bij maatwijzigingen is de hogere lat. Houd sketches volledig bepaald en bouw relaties bewust op.
- **Visuele verificatie is niet onfeilbaar.** Gebruik screenshots als aanvulling, niet als enige waarheid.

## 7. Voorgestelde milestones

- **M0 — Verbinding:** `connect`/`get_status` werkt; agent kan een draaiende SolidWorks zien en de actieve doc rapporteren.
- **M1 — Eén blok:** `new_part` → `create_sketch` (rechthoek) → `add_extrude` → `get_mass_properties`. Volume klopt met handberekening.
- **M2 — Parametrisch:** `set_dimension` wijzigt een maat, `rebuild`, mass properties veranderen voorspelbaar. Dit bewijst de parametrische loop.
- **M3 — Verificatie-loop:** agent krijgt een spec ("blok 40×20×10 mm, gat Ø8 centraal"), bouwt, meet, corrigeert tot het klopt, exporteert STL.
- **M4 — Uitbreiding:** revolve, fillet, equations, meerdere features; screenshot-verificatie.
- **M5 — Later:** assemblies, interference detection, drawings, SolidWorks Simulation (FEA) in de loop.

## 8. Tips voor de Claude Code-sessie

- Zet `swApp.Visible = True` zodat je elke stap live in SolidWorks ziet — onmisbaar bij het debuggen van de loop.
- Begin met M0–M1 volledig werkend voordat je tools toevoegt. Het skelet (verbinding + bouwen + meten) draagt al de rest.
- Log elke COM-call en return-waarde tijdens ontwikkeling; de API faalt vaak stil.
- Houd één klein testpart als referentie waarmee je `measure`/`mass_properties` valideert tegen bekende waarden.
- Raadpleeg de officiële **SOLIDWORKS API Help** voor exacte signaturen; de genummerde functievarianten verschillen per versie.

---
*Opgesteld als startdocument voor het SolidWorks-MCP-project. Pas scope en milestones gerust aan op je eigen prioriteiten.*
