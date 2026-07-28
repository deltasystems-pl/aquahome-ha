# AquaHome — zmiękczacz wody iQua w Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2026.2%2B-41BDF5.svg)](https://www.home-assistant.io/)
[![Licencja: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Ta integracja przenosi do Home Assistant zmiękczacz wody obsługiwany przez
aplikację **iQua** — linie **Viessmann Aquahome** (Aquahome 20/30 Smart),
inteligentne zmiękczacze **EcoWater** tej samej generacji i inne urządzenia tej
platformy sprzedawane pod różnymi markami. Jeśli steruje nim aplikacja iQua,
powinna też ta integracja.

Dostajesz wszystkie dane widoczne w aplikacji: poziom soli, zużycie wody, stan
regeneracji, alerty i ustawienia urządzenia. Do tego historię zużycia wody
zaimportowaną z chmury — także sprzed instalacji — gotową do wpięcia w panel
**Energia**; codzienną analizę tej historii, czyli podejrzenie wycieku,
nietypowe zużycie, wykrytą nieobecność domowników i prognozę na jutro; oraz
tryb podglądu na żywo, w którym licznik wody i przepływ zmieniają się w ciągu
sekund, a nie co dziesięć minut.

Czego integracja **nie** robi. Nie łączy się ze zmiękczaczem lokalnie — tak
samo jak aplikacja, rozmawia z chmurą producenta, więc bez internetu nie
zobaczysz nic. Nie zmienia też niczego w urządzeniu sama z siebie: żadne
polecenie nie trafia do zmiękczacza, dopóki nie naciśniesz przycisku albo
świadomie nie włączysz jednego z przełączników automatyzacji (wszystkie są
domyślnie wyłączone). Nie zastąpi hydraulika ani sprzętowego czujnika zalania.

## Czego potrzebujesz

- **Home Assistant 2026.2.0** lub nowszy.
- Konto w aplikacji **iQua** — ten sam adres e-mail i hasło, którymi logujesz
  się w telefonie — z co najmniej jednym urządzeniem.
- Dostęp Home Assistanta do internetu.

Nic więcej: żadnego dodatkowego sprzętu, żadnych wpisów w `configuration.yaml`.

## Instalacja przez HACS

HACS to sklep z dodatkami społeczności. Ta integracja nie jest jeszcze w jego
domyślnym katalogu, więc trzeba ją dodać jako **repozytorium niestandardowe** —
brzmi groźnie, ale to jedno kliknięcie:

[![Otwórz to repozytorium w HACS na swojej instancji Home Assistant.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=deltasystems-pl&repository=aquahome-ha&category=integration)

Kliknij odznakę powyżej — otworzy się HACS na twojej instancji z już wpisanym
adresem repozytorium. Naciśnij **Pobierz** (Download). Gdyby odznaka nie
zadziałała, zrób to samo ręcznie:

1. W Home Assistant otwórz **HACS**.
2. Menu **⋮** w prawym górnym rogu → **Custom repositories**.
3. Wklej adres `https://github.com/deltasystems-pl/aquahome-ha` i wybierz typ
   **Integration**.
4. Znajdź na liście **AquaHome (iQua water softener)** i naciśnij **Pobierz**.

**Na koniec zrestartuj Home Assistanta** (**Ustawienia → System → przycisk
zasilania → Uruchom ponownie Home Assistant**). Restart jest konieczny, bo Home
Assistant wczytuje integracje społecznościowe tylko przy starcie: HACS
skopiował pliki na dysk, ale dopóki się nie zrestartujesz, nic o nich nie wie.

Instalacja ręczna (skopiowanie katalogu do `config/custom_components/`) też
jest możliwa — opisuje ją angielski [README.md](README.md).

## Konfiguracja

1. **Ustawienia → Urządzenia i usługi → Dodaj integrację**, wpisz **AquaHome**.
2. Podaj **adres e-mail i hasło** z aplikacji iQua. To cały formularz.
3. Jeśli twoje konto tego wymaga, pojawi się krok **Zweryfikuj konto** — na
   skrzynkę przyjdzie kod potwierdzający, przepisz go i gotowe.
4. Integracja sama znajdzie wszystkie urządzenia na koncie i utworzy encje.

**Dlaczego nie ma nic więcej do wybrania?** Konta iQua obsługuje jeden z dwóch
serwerów producenta, a integracja przy logowaniu sprawdza oba i zapamiętuje
ten, który odpowiedział — pytanie o „typ API” byłoby pytaniem, na które i tak
nikt nie potrafi odpowiedzieć.

Hasło nie jest przechowywane: zostaje wymienione na tokeny i tylko one są
zapisane. Gdyby przestały działać, Home Assistant sam poprosi o ponowne
uwierzytelnienie, a historia encji zostaje zachowana. Urządzenia z pierwszej
generacji chmury EcoWater (obsługiwane starą aplikacją EcoWater, a nie iQua)
korzystają z innego API i nie są wspierane.

## Co dostajesz

Przewodnik po tym, co pojawi się na stronie urządzenia, pogrupowany tak samo
jak [gotowy dashboard](dashboards/aquahome-dashboard.yaml). Ile encji
dostaniesz, zależy od modelu i wyposażenia — urządzenie referencyjne (AquaHome
20 Smart bez zaworu odcinającego i bez czujników wycieku) ma ich 83. Pełny opis
wszystkich encji jest w [`docs/entities.md`](docs/entities.md) (na razie tylko
po angielsku).

### Woda

**Zużycie wody dzisiaj** i **Łączne zużycie wody** to liczniki: dzisiejszy
i ten liczony od zawsze. **Dostępna woda zmiękczona** mówi, ile miękkiej wody
zostało do następnej regeneracji, a **Pozostała pojemność jonowymienna** to
samo w procentach. Jest też **Średnie dobowe zużycie wody** i siedem średnich
dla dni tygodnia (**Średnie zużycie w soboty** i pozostałe) — te prowadzi sam
zmiękczacz i odświeża tylko w danym dniu, więc bywają nieaktualne nawet o kilka
tygodni. **Przepływ wody** działa naprawdę tylko podczas sesji podglądu na
żywo; poza nią zwykle pokazuje 0, mimo że licznik dzienny rośnie
([dlaczego](#częste-pytania)).

### Analiza

Raz na dobę, o 07:35 czasu lokalnego urządzenia, integracja analizuje
zaimportowaną historię i publikuje pięć encji: **Podejrzenie wycieku**,
**Anomalia zużycia**, **Wykryta nieobecność**, **Prognoza zużycia wody**
i **Przepływ nocny**. Wszystkie są wyłącznie do odczytu — analiza nigdy niczego
nie robi ze zmiękczaczem.

Liczy przy tym doby **od południa do południa**, a nie od północy: dom zużywa
wodę wieczorem i w nocy, a cięcie o północy rozdzieliłoby wieczór od jego nocy,
więc jej dobowe sumy różnią się od słupków w panelu Energia. Gdy nie ma czego
oceniać, te czujniki pokazują *nieznany* zamiast *wyłączony* — „nie znalazłem
wycieku” i „nie umiem tego ocenić” to dwie różne odpowiedzi. Wykrywalność ma
też próg: wodomierz liczy pełne galony i melduje się tylko wtedy, gdy woda
płynie, więc kapanie wolniejsze niż około 1 galon na godzinę (jakieś 91 litrów
na dobę) jest dla niej niewidoczne.

### Na żywo

**Tryb podglądu na żywo** pokazuje, czy trwa sesja: *Bezczynny*, *Na żywo* albo
*Wstrzymany po błędzie*. Sterują nią trzy przełączniki opisane
[niżej](#trzy-przełączniki-podglądu-na-żywo) oraz liczby **Sesje podglądu na
żywo dziennie** i **Minimalny odstęp między sesjami podglądu**. Podgląd na żywo
**nie dodaje nowych czujników** — przyspiesza te, które masz: w trakcie sesji
liczniki wody, **Przepływ wody** i **Pozostały czas regeneracji** zmieniają się
w ciągu sekund. Sesję otwiera też sama integracja (start regeneracji, zapalona
**Anomalia zużycia**, skok licznika o co najmniej 2 galony) — to normalne.

### Regeneracja

**Stan regeneracji** mówi, co się dzieje: *Nieaktywna*, *Zaplanowana*,
*Regeneracja w toku*, *Wstrzymana*, *Wyłączona* albo *Błąd*. Obok są **Następna
regeneracja**, **Ostatnia regeneracja**, **Dni od ostatniej regeneracji**,
**Łączna liczba regeneracji** i **Pozostały czas regeneracji** — ten ostatni
pokazuje 0, gdy cykl nie trwa, bo chmura zostawia po sobie starą wartość,
a integracja ją zeruje, żebyś nie oglądał „42 minuty do końca” pięć godzin po
fakcie. Przyciski **Regeneruj teraz**, **Zaplanuj regenerację** i **Anuluj
regenerację** robią to, co obiecują, i tylko wtedy, gdy je naciśniesz.

### Sól

**Poziom soli** to procent zapełnienia zbiornika tak, jak podaje go urządzenie,
a **Alert poziomu soli** zapala się, gdy zmiękczacz sam uzna, że soli jest
mało. Do tego liczniki: **Łączne zużycie soli**, **Zużycie soli na
regenerację**, **Szacowane dobowe zużycie soli** i **Efektywność zużycia
soli**.

Dlaczego są **dwie** informacje o zapasie soli? **Przewidywany brak soli** to
własne odliczanie zmiękczacza przeliczone na datę — sygnał podstawowy, na nim
opierają się powiadomienia (ostrzeżenie przy 14 dniach, mocniejsze przy 7).
**Szacowane dni zapasu soli** to niezależna kontrolka: integracja przelicza ten
sam zapas według *bieżącego* zużycia wody i twardości, więc reaguje szybciej —
gdy zużycie się zmieni, ta liczba ruszy pierwsza, a odliczanie urządzenia
dogoni ją później. Czytaj ją obok wskazania urządzenia, a nie zamiast niego.

### Automatyka

Trzy przełączniki: **Odroczenie regeneracji (urlop)**, **Automatyczne
odroczenie regeneracji (urlop)** i **Inteligentne planowanie regeneracji**.
Wszystkie są domyślnie wyłączone, opisuje je sekcja [Co integracja robi
z urządzeniem](#co-integracja-robi-z-urządzeniem). Są przy tym dostępne zawsze,
także gdy chmura milczy: trzymają *twoje* ustawienie, nie stan urządzenia, więc
awaria nigdy nie odbierze ci możliwości wyłączenia automatyzacji.

### Alarmy

Encja **Alert** wystawia każdy nowy alert z chmury (widać go też w dzienniku
zdarzeń), a **Ostatni alert** trzyma treść ostatniego. Obok są flagi, które
zmiękczacz podnosi sam: **Alert kodu błędu**, **Alert monitora przepływu**,
**Alert zużycia wody**, **Alert złoża żywicy** i **Alert połączenia** — celowo
pozostają dostępne również wtedy, gdy zmiękczacz zniknie z sieci, bo właśnie
wtedy chcesz je móc odczytać. Alerty sprzed instalacji nie są odtwarzane, więc
restart nie zasypie cię powiadomieniami sprzed miesiąca.

### Urządzenie

**Online** mówi, czy zmiękczacz jest osiągalny przez chmurę, i to od niego
zależy dostępność większości pozostałych encji. Dalej dane techniczne —
**Model**, **Numer seryjny**, **Oprogramowanie sterownika**, **Oprogramowanie
modułu Wi-Fi**, **Dni od włączenia zasilania** — oraz przycisk **Odśwież
dane**, który prosi urządzenie o natychmiastowe wysłanie stanu, gdy nie chcesz
czekać do kolejnego odpytania. Ustawienia urządzenia (twardość wody, godzina
regeneracji, rodzaj soli, tryb efektywności i inne) pojawiają się jako listy
wyboru w sekcji *Konfiguracja*, nazwane wprost przez chmurę, w języku twojego
Home Assistanta.

## Trzy przełączniki podglądu na żywo

Zwykle integracja pyta chmurę o nowości co dziesięć minut. Sesja podglądu na
żywo to krótkie bezpośrednie połączenie, w trakcie którego zmiękczacz melduje
każdy galon na bieżąco. Otwierają ją trzy przełączniki:

- **Podgląd na żywo** — ręczne „pokaż mi teraz”. Trzyma sesję otwartą, dopóki
  go nie wyłączysz, najdalej przez 30 minut, po czym wyłącza się sam — podobnie
  jak wtedy, gdy sesji nie da się kontynuować (urządzenie wypadło z sieci,
  chmura odmawia). To celowo rzecz jednorazowa; gdy znów chcesz danych na żywo,
  włącz go ponownie.
- **Inteligentne okna podglądu** — opcja dla chętnych, domyślnie wyłączona.
  Integracja uczy się, w których godzinach twoje gospodarstwo naprawdę zużywa
  wodę w poszczególne dni tygodnia, i na początku takiej godziny otwiera sesję,
  trzymając ją przez całą godzinę — pobór zostaje wtedy zapisany galon po
  galonie, zamiast być spróbkowany raz na dziesięć minut. Nie włącza się nigdy
  między 01:00 a 07:00, a gdy trzy okna z rzędu nie zobaczą ani kropli,
  odpuszcza do końca dnia.
- **Ciągły pomiar przepływu** — opcja maksymalna, domyślnie wyłączona. Trzyma
  sesję otwartą bez końca, wznawiając ją, gdy urządzenie zamknie swoje okno
  raportowania. To znaczy rozmawiać z chmurą producenta całą dobę.

Wszystkie sesje — także te otwierane automatycznie — dzielą jeden budżet:
domyślnie **48 sesji na dobę** i **minimum 120 sekund przerwy** między nimi
(wznowienia w ramach już otwartej sesji się nie liczą). Oba zmienisz encjami
**Sesje podglądu na żywo dziennie** (4–200) i **Minimalny odstęp między sesjami
podglądu** (60–900 s) — również w dół. Gdy budżet się wyczerpie albo chmura
zacznie ograniczać żądania, sesja po prostu się nie otworzy, a zwykłe
odpytywanie leci dalej.

**Uczciwie o obciążeniu chmury.** Odpytywanie co 10 minut jest ustalone na
sztywno i nie ma suwaka, którym dałoby się je przyspieszyć. To decyzja, a nie
niedopatrzenie: chmura iQua ostro limituje żądania, a użytkownicy innych
narzędzi tracili dostęp do kont za agresywne odpytywanie. Tryb podglądu na żywo
istnieje właśnie po to, żeby „chcę to zobaczyć teraz” nie zamieniało się
w „odpytuj szybciej na zawsze”.

## Co integracja robi z urządzeniem

Krótko: nic, dopóki jej nie poprosisz.

- **Analiza dobowa niczego nie dotyka.** Czyta historię i publikuje czujniki.
- **Przyciski działają tylko po naciśnięciu.** Regeneracja, anulowanie,
  wyciszenie alarmu — to twoje kliknięcie.
- **Trzy przełączniki automatyzacji są domyślnie wyłączone.** *Odroczenie
  regeneracji (urlop)* anuluje zaplanowane regeneracje, gdy nikogo nie ma
  w domu — po 21 dniach odroczenia jedna regeneracja i tak zostanie
  przepuszczona, żeby chronić złoże żywicy, a anulowań jest najwyżej trzy
  dziennie. *Automatyczne odroczenie regeneracji (urlop)* pozwala wykrywaniu
  nieobecności sterować tym pierwszym przełącznikiem, ale odroczenia
  ustawionego ręcznie nigdy nie zdejmie. *Inteligentne planowanie regeneracji*
  planuje regenerację, gdy zapas miękkiej wody spadnie poniżej jutrzejszej
  prognozy powiększonej o 50% rezerwy.
- **Podpowiedzi wymagają potwierdzenia.** Dwie sugestie przychodzą jako
  zgłoszenia w sekcji *Naprawy*: „wygląda na to, że nikogo nie ma — odroczyć
  regeneracje?” oraz „godzina regeneracji pokrywa się z porą, w której naprawdę
  zużywacie wodę — przesunąć ją?”. Żadna nic nie zapisze, dopóki nie naciśniesz
  przycisku.

Żeby nie było nieporozumień: **tryb urlopowy w tej integracji oznacza
odraczanie regeneracji po stronie Home Assistanta**. Nie przełącza kafelka
trybu urlopowego w aplikacji iQua — format tego polecenia nie został
potwierdzony, więc integracja go nie wysyła. Czujnik **Tryb urlopowy** nadal
pokazuje własny stan urządzenia.

## Historia wody w panelu Energia

Normalnie statystyki w Home Assistant zaczynają się w dniu, w którym powstała
encja. Chmura iQua trzyma jednak historię licznika, więc integracja importuje
ją jako osobną serię statystyk, tak głęboko wstecz, jak sięgają zapisy.

[![Otwórz konfigurację Energii na swojej instancji Home Assistant.](https://my.home-assistant.io/badges/config_energy.svg)](https://my.home-assistant.io/redirect/config_energy/)

1. Otwórz konfigurację Energii (odznaka powyżej albo **Ustawienia → Panele →
   Energia**).
2. W sekcji **Zużycie wody** naciśnij **Dodaj źródło wody**.
3. Wybierz pozycję o nazwie kończącej się na *water usage history* (np. „Dom
   water usage history”). Ta nazwa pochodzi z importu i jest po angielsku — nie
   znajdziesz jej na liście encji, bo to nie jest encja.
4. Zapisz. Zakładka **Woda** wypełni się danymi wstecz.

**Wybierz tylko jedno źródło.** Zaimportowana historia i czujnik **Łączne
zużycie wody** liczą tę samą wodę — dodanie obu (albo dorzucenie licznika
z innej integracji dla tego samego wodomierza) sprawi, że panel policzy
wszystko podwójnie. Historia z importu jest lepsza z dwóch powodów: obejmuje
miesiące sprzed instalacji i powstaje z godzinowych zapisów wodomierza, podczas
gdy **Łączne zużycie wody** bywa wysyłane z opóźnieniem i nadrabia jednym
skokiem — a taki skok ląduje na wykresie jako jedna gigantyczna godzina.

## Gotowy dashboard i etykiety

Strona urządzenia grupuje encje tylko według rodzaju (sterowanie, czujniki,
konfiguracja, diagnostyka). Jeśli wolisz podział według funkcji — woda,
analiza, na żywo, regeneracja, sól, automatyka, alarmy, urządzenie —
w repozytorium czeka gotowy plik
[`dashboards/aquahome-dashboard.yaml`](dashboards/aquahome-dashboard.yaml).
Utwórz pusty dashboard (**Ustawienia → Panele → Dodaj dashboard**), otwórz
edytor surowej konfiguracji (menu **⋮** → *Raw configuration editor*), wklej
plik i zamień w nim `NICK` na identyfikator swojego urządzenia — instrukcja
jest w nagłówku pliku.

Do tego samego dobrze nadają się **etykiety**: nazwij je jak te grupy, przypisz
do nich encje i filtruj po nich w całym Home Assistant. Etykiety należą do
ciebie, więc integracja celowo nie nadaje żadnych z góry.

## Częste pytania

**Dlaczego „Przepływ wody” pokazuje 0, choć leci mi woda z kranu?**

Bo zmiękczacz nie raportuje przepływu w sposób ciągły — wysyła wiadomość na
początku i na końcu poboru, a między nimi milczy. Odpytywanie co dziesięć minut
prawie nigdy nie trafia w środek poboru, więc widzisz ostatnią rzecz, jaką
urządzenie powiedziało: zwykle zamykające zero. Włącz **Podgląd na żywo**,
a czujnik zacznie działać jak prawdziwy wskaźnik. A ile wody faktycznie poszło
— to i tak wiernie pokazują liczniki objętości.

**Dlaczego niektórych encji nie widzę?**

Albo twoje urządzenie nie ma danego wyposażenia (zaworu odcinającego, alarmu
dźwiękowego, czujników wycieku) i encja w ogóle nie powstaje, albo chmura nie
przysyła dla twojego modelu tego bloku danych, albo encja jest **wyłączona
domyślnie** — tak jest z rzeczami serwisowymi i rzadko przydatnymi (np. **Siła
sygnału RF** czy **Szacowana data wyczerpania soli**). Żeby taką encję włączyć:
**Ustawienia → Urządzenia i usługi → AquaHome → twoje urządzenie**, znajdź ją
na liście, otwórz ustawienia (ikona koła zębatego) i przestaw **Włączone**.
Sprzęt dokupiony później dorobi swoje encje sam, w ciągu mniej więcej
dwudziestu minut.

**Czy to obciąża chmurę iQua? Czy grozi mi blokada konta?**

Integracja jest celowo oszczędna: stan urządzenia odpytuje co 10 minut, alerty
i historię co 30 minut, ustawienia co 6 godzin, a historię zużycia co 12 godzin
— i tego rytmu nie da się przyspieszyć, bo nie ma takiej opcji. Sesje podglądu
na żywo mają własny budżet (domyślnie 48 na dobę, minimum 120 sekund przerwy),
z zapasem mieszczący się w zmierzonych limitach. Uczciwie: to nieoficjalne API
producenta, a producent może zmienić zasady bez uprzedzenia — dlatego
ustawienia domyślne są ostrożne, a limity wystawione jako encje, żebyś mógł je
jeszcze obniżyć.

**Co się dzieje, gdy chmura nie odpowiada?**

Krótkie zakłócenia nie powodują migotania encji — integracja podaje ostatni
dobry odczyt jeszcze przez 30 minut (dane urządzenia), 3 godziny (alerty
i historia) albo 24 godziny (ustawienia). Dopiero potem uczciwie przyznaje, że
jest *niedostępna*, zamiast pokazywać wczorajsze liczby jako dzisiejsze.
Przełączniki i liczby podglądu na żywo, przełączniki automatyzacji oraz **Tryb
podglądu na żywo** są dostępne zawsze — trzymają twoje ustawienia, nie stan
urządzenia. Gdy zawiedzie samo połączenie na żywo, integracja odczekuje
(minutę, potem dłużej, najwyżej 30 minut) i wraca do zwykłego odpytywania;
zgłoszenie w sekcji *Naprawy* pojawia się dopiero po pięciu nieudanych próbach
z rzędu i znika samo, gdy sesja znów się powiedzie.

## Więcej

Pełna dokumentacja techniczna — akcje, zdarzenia, gotowe szablony automatyzacji
(blueprinty), przykłady YAML i lista znanych ograniczeń — jest w angielskim
[README.md](README.md), a opis wszystkich encji
w [`docs/entities.md`](docs/entities.md). Błędy i pytania zgłaszaj przez
[GitHub Issues](https://github.com/deltasystems-pl/aquahome-ha/issues); jeśli
masz zawór odcinający albo czujniki wycieku, zgłoś się koniecznie — ta część
integracji nie została jeszcze sprawdzona na prawdziwym sprzęcie.

Wszystkie nazwy produktów i znaki towarowe należą do ich właścicieli. Ten
projekt nie jest powiązany z iQua, EcoWater Systems ani AquaHome, nie jest
przez nie wspierany ani firmowany. Korzystasz z niego na własną
odpowiedzialność. Licencja: [MIT](LICENSE).
