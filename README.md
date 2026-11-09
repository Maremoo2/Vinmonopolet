# Vinmonopolet Wine Intelligence

Privat produktkatalog og kobling mot egen vinkjeller. Klienten bruker
[Vinmonopolets Open-API](https://api.vinmonopolet.no/api-details#api=products&operation=GET_DETAILS_NORMAL).

## Kom i gang

Bruk Python 3.10 eller nyere.

```powershell
python -m pip install -r requirements.txt
python wine_intelligence.py probe
python wine_intelligence.py sync
```

Ved interaktiv kjøring spør programmet etter Open-nøkkelen med skjult inntasting.
Ved automatisering kan nøkkelen gis via miljøvariabelen `VINMONOPOLET_API_KEY`.
Bruk et privat, kryptert lagringssted for databasen. Med `--database` før
underkommandoen kan du velge en annen plassering enn `data/catalog.sqlite3`.

## Hva kommandoene gjør

- `probe` henter to små sider og viser HTTP-status og antall rader. Legg til ett
  eller flere varenummer som argumenter for å sjekke dekningen for kjente varer.
- `sync` henter alle sider første gang og bruker deretter `changedSince` med én
  dags overlapp. `sync --full` starter en ny full lesing. Ved feil rulles importen
  tilbake. Et endret API-tidsstempel registrerer en ny observasjon, men avslører
  ikke nødvendigvis hvilket produktfelt som ble endret.
- `cellar kjeller.csv` skriver en treffliste for en UTF-8 CSV-fil med komma og
  kolonnen `varenummer`. Varenummer sammenlignes som tekst og kjellerfilen endres
  ikke. Importerte varer kan fortsatt gi treff etter at de har gått ut av utvalget.

## Datadekning og kilder

`details-normal` omfatter basis-, parti-, test- og bestillingsutvalget på tvers
av varegrupper. Den dokumenterte responsen har varenummer, kortnavn og sist endret.
Pris, årgang, lager og et entydig felt for utgått vare må bekreftes i en egen,
tillatt datakilde før de kan brukes. Historikken starter med første import.

[Vinmonopolets oversikt over datadeling](https://www.vinmonopolet.no/om-oss/presse/datadeling)
og [API-vilkår](https://api.vinmonopolet.no/terms-of-service) beskriver tilgang og
bruk av data. Kall kjøres sekvensielt med pause. Kallbudsjettet i programmet
gjelder per kjøring, så ta også høyde for annen bruk av abonnementet samme dag.

Gjennomgåtte prosjekter inkluderer [vinmonopolet](https://github.com/rexxars/vinmonopolet)
for pagineringsmønster, [gowine](https://github.com/erikbjo/gowine) og
[snublejuice](https://github.com/hallvardnmbu/snublejuice) for prisoppfølging,
samt [tappdopolet](https://github.com/Boren/tappdopolet),
[olmonopolet-app](https://github.com/haavardnk/olmonopolet-app) og
[polet-pluss](https://github.com/itzjacki/polet-pluss) for kobling av
produktoppføringer og visning. Implementasjonen her er skrevet mot den offisielle
Open-dokumentasjonen.
