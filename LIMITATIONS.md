# Nicht umgesetzt / Einschränkungen

Dieses Dokument hält fest, was **bewusst nicht** umgesetzt wurde (und warum)
sowie welche Einschränkungen beim Einsatz gelten.

## Bewusst nicht umgesetzt

### Kein Bank-/Depot-Connector
Der Bot arbeitet ausschließlich mit den im Dialog erfragten Angaben. Eine
Anbindung an echte Konten/Depots (z. B. via FinTS/PSD2-APIs wie GoCardless,
finAPI) wurde bewusst weggelassen:
- **Regulatorik:** Kontoinformationsdienste benötigen eine BaFin-Registrierung
  (ZAG); das ist für ein Hochschulprojekt weder möglich noch angemessen.
- **Sicherheit:** Umgang mit Bank-Credentials/Tokens erfordert ein
  Sicherheitsniveau (Secret-Handling, Verschlüsselung, Audit), das den
  Projektrahmen sprengt.
- **Aufwand/Nutzen:** Für die Beratungsqualität genügen die Dialog-Angaben.

### Keine zugelassene Anlageberatung
Anlageberatung im rechtlichen Sinn (§ 1 Abs. 1a Nr. 1a KWG / WpHG) erfordert
eine Erlaubnis und u. a. Geeignetheitserklärungen. Der Bot ist als
**allgemeine Informationsanwendung** konzipiert: Er gibt keine individuellen
Empfehlungen im aufsichtsrechtlichen Sinn ab, verspricht keine Renditen und
weist zu Gesprächsbeginn und in jeder Strategie darauf hin.

### Steuer nur als vereinfachte Schätzung
Der Umschichtungsplan schätzt Steuern auf Verkäufe bewusst vereinfacht:
pauschal 26,375 % (Abgeltungsteuer + Soli) auf den anteiligen Gewinn, 30 %
Teilfreistellung bei Aktienfonds. **Nicht berücksichtigt:** Kirchensteuer,
Günstigerprüfung/persönlicher Steuersatz, bereits verbrauchter
Sparer-Pauschbetrag, FIFO-Reihenfolge bei Teilverkäufen (geschätzt wird
proportional), Altbestände vor 2009, bereits versteuerte Vorabpauschalen
(würden den steuerpflichtigen Gewinn mindern) sowie die Trennung der
Verlustverrechnungstöpfe (Aktien vs. Sonstige). Eine korrekte individuelle
Steuerrechnung hängt von persönlichen Umständen ab und wäre faktisch
Steuerberatung – die Schätzungen dienen der Größenordnung, nicht der
Steuererklärung.

### Keine Order-/Ausführungsfunktion
Der Bot platziert keine Käufe und verlinkt nicht auf Affiliate-Angebote –
er endet bewusst bei der Strategie und deren Begründung.

## Technische Einschränkungen

- **Session-State pro Serverprozess:** `agent.to_web(deps=...)` verwendet ein
  gemeinsames Deps-Objekt für alle Requests. Für den vorgesehenen Einsatz
  (lokale Einzelnutzer-App) ist das korrekt; parallele Chats im selben Server
  teilen sich jedoch das Profil, und ein Server-Neustart leert es
  (kein Persistenz-Backend wie SQLite – als Erweiterungspunkt denkbar).
- **Schlüssellose Datenquellen:** DuckDuckGo-Suche und Yahoo Finance sind
  inoffizielle bzw. ratenbegrenzte Quellen. Kennzahlen wie TER sind dort nicht
  immer verfügbar; der Bot ist angewiesen, fehlende Angaben offen zu
  kennzeichnen statt sie zu erfinden. Für Produktionsqualität wären
  lizenzierte Datenfeeds nötig.
- **Kapitalmarktannahmen sind Modellannahmen:** Die erwarteten Renditen,
  Volatilitäten und Korrelationen in `risk.py` sind konservative, gerundete
  Langfristwerte (im Skript Kap. 3 als „Beschränkung der Asset-Allokation:
  Datenqualität“ thematisiert). Sie bestimmen das Nutzenoptimum und sind
  bewusst zentral und änderbar abgelegt.
- **Zwei-Anlagen-Optimierung:** Das Bernoulli-/Markowitz-Optimum wird für den
  didaktisch klaren Zwei-Anlagen-Fall (riskant vs. defensiv) gerechnet; die
  Feinaufteilung (Welt/EM, Anleihen/Geldmarkt, Gold) folgt regelbasiert. Eine
  vollständige Mehr-Asset-Optimierung (quadratische Programmierung) wäre ein
  Ausbau-Schritt, bringt aber bei geschätzten Inputs kaum belastbaren
  Mehrwert („Garbage in, garbage out“).
- **LLM-Abhängigkeit der Dialogqualität:** Die Phasenlogik steckt im
  System-Prompt. Kleine Modelle können Phasen überspringen; das Zahlenwerk
  bleibt dank Tool-Berechnung trotzdem konsistent, und die Tools verweigern
  die Strategie, solange das Profil unvollständig ist.
- **Keine automatisierten Tests der Dialogführung:** Die Fachlogik
  (risk/strategy) ist deterministisch und manuell verifiziert; LLM-Dialoge
  sind nicht CI-testbar ohne API-Kosten.
