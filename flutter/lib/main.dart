import 'dart:async';

import 'package:flutter/material.dart';
import 'package:local_auth/local_auth.dart';
import 'package:mobile_scanner/mobile_scanner.dart';
import 'package:path/path.dart' as p;
import 'package:sqflite/sqflite.dart';
import 'package:uuid/uuid.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const CheckInQrApp());
}

class CheckInQrApp extends StatelessWidget {
  const CheckInQrApp({super.key});

  @override
  Widget build(BuildContext context) {
    const seed = Color(0xFF14B8A6);
    return MaterialApp(
      debugShowCheckedModeBanner: false,
      title: 'CheckInQR',
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(
          seedColor: seed,
          brightness: Brightness.dark,
        ),
        scaffoldBackgroundColor: const Color(0xFF0B1120),
        cardTheme: const CardThemeData(
          color: Color(0xFF111827),
          margin: EdgeInsets.zero,
        ),
        inputDecorationTheme: const InputDecorationTheme(
          border: OutlineInputBorder(),
          filled: true,
          fillColor: Color(0xFF0F172A),
        ),
        useMaterial3: true,
      ),
      home: const AppBootstrap(),
    );
  }
}

class AppBootstrap extends StatefulWidget {
  const AppBootstrap({super.key});

  @override
  State<AppBootstrap> createState() => _AppBootstrapState();
}

class _AppBootstrapState extends State<AppBootstrap> {
  late final Future<LocalStore> _storeFuture = LocalStore.open();

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<LocalStore>(
      future: _storeFuture,
      builder: (context, snapshot) {
        if (!snapshot.hasData) {
          return const Scaffold(
            body: Center(child: CircularProgressIndicator()),
          );
        }
        return HomeShell(store: snapshot.data!);
      },
    );
  }
}

enum SyncMode {
  localOnly('Solo este dispositivo'),
  mainDevice('Dispositivo principal local'),
  dockerServer('Servidor Docker/VPS'),
  googleService('Google/Firebase');

  const SyncMode(this.label);
  final String label;
}

class Guest {
  Guest({
    required this.uuid,
    required this.name,
    required this.guestType,
    required this.passes,
    required this.amountPaid,
    required this.raffleNumber,
    this.parentUuid,
    this.phone = '',
    this.email = '',
    this.role = '',
    this.school = '',
    this.checkedInAt,
    this.syncStatus = 'pending',
  });

  final String uuid;
  final String? parentUuid;
  final String guestType;
  final String name;
  final String phone;
  final String email;
  final int passes;
  final String role;
  final String school;
  final double amountPaid;
  final int raffleNumber;
  final DateTime? checkedInAt;
  final String syncStatus;

  bool get isCheckedIn => checkedInAt != null;
  bool get isHolder => parentUuid == null;

  Guest copyWith({
    DateTime? checkedInAt,
    String? syncStatus,
  }) {
    return Guest(
      uuid: uuid,
      parentUuid: parentUuid,
      guestType: guestType,
      name: name,
      phone: phone,
      email: email,
      passes: passes,
      role: role,
      school: school,
      amountPaid: amountPaid,
      raffleNumber: raffleNumber,
      checkedInAt: checkedInAt ?? this.checkedInAt,
      syncStatus: syncStatus ?? this.syncStatus,
    );
  }

  Map<String, Object?> toMap() {
    return {
      'uuid': uuid,
      'parent_uuid': parentUuid,
      'guest_type': guestType,
      'name': name,
      'phone': phone,
      'email': email,
      'passes': passes,
      'role': role,
      'school': school,
      'amount_paid': amountPaid,
      'raffle_number': raffleNumber,
      'checked_in_at': checkedInAt?.toIso8601String(),
      'sync_status': syncStatus,
    };
  }

  factory Guest.fromMap(Map<String, Object?> map) {
    final checkedValue = map['checked_in_at'] as String?;
    return Guest(
      uuid: map['uuid'] as String,
      parentUuid: map['parent_uuid'] as String?,
      guestType: map['guest_type'] as String,
      name: map['name'] as String,
      phone: (map['phone'] as String?) ?? '',
      email: (map['email'] as String?) ?? '',
      passes: (map['passes'] as int?) ?? 1,
      role: (map['role'] as String?) ?? '',
      school: (map['school'] as String?) ?? '',
      amountPaid: ((map['amount_paid'] as num?) ?? 0).toDouble(),
      raffleNumber: (map['raffle_number'] as int?) ?? 0,
      checkedInAt: checkedValue == null ? null : DateTime.parse(checkedValue),
      syncStatus: (map['sync_status'] as String?) ?? 'pending',
    );
  }
}

class CheckInEvent {
  CheckInEvent({
    required this.id,
    required this.guestUuid,
    required this.staffUser,
    required this.deviceId,
    required this.checkedInAt,
    required this.syncStatus,
  });

  final int? id;
  final String guestUuid;
  final String staffUser;
  final String deviceId;
  final DateTime checkedInAt;
  final String syncStatus;

  Map<String, Object?> toMap() {
    return {
      'id': id,
      'guest_uuid': guestUuid,
      'staff_user': staffUser,
      'device_id': deviceId,
      'checked_in_at': checkedInAt.toIso8601String(),
      'sync_status': syncStatus,
    };
  }
}

class LocalStore {
  LocalStore._(this.db);

  final Database db;
  final String deviceId = const Uuid().v4();

  static Future<LocalStore> open() async {
    final dbPath = await getDatabasesPath();
    final db = await openDatabase(
      p.join(dbPath, 'checkinqr_mobile.db'),
      version: 1,
      onCreate: (db, version) async {
        await db.execute('''
          CREATE TABLE guests (
            uuid TEXT PRIMARY KEY,
            parent_uuid TEXT,
            guest_type TEXT NOT NULL,
            name TEXT NOT NULL,
            phone TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            passes INTEGER NOT NULL DEFAULT 1,
            role TEXT NOT NULL DEFAULT '',
            school TEXT NOT NULL DEFAULT '',
            amount_paid REAL NOT NULL DEFAULT 0,
            raffle_number INTEGER NOT NULL DEFAULT 0,
            checked_in_at TEXT,
            sync_status TEXT NOT NULL DEFAULT 'pending'
          )
        ''');
        await db.execute('''
          CREATE TABLE checkin_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guest_uuid TEXT NOT NULL,
            staff_user TEXT NOT NULL,
            device_id TEXT NOT NULL,
            checked_in_at TEXT NOT NULL,
            sync_status TEXT NOT NULL DEFAULT 'pending'
          )
        ''');
        await db.execute('''
          CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
          )
        ''');
      },
    );
    final store = LocalStore._(db);
    await store.seedIfEmpty();
    return store;
  }

  Future<void> seedIfEmpty() async {
    final count = Sqflite.firstIntValue(
      await db.rawQuery('SELECT COUNT(*) FROM guests'),
    );
    if ((count ?? 0) > 0) return;
    const uuid = Uuid();
    final holderUuid = uuid.v4();
    final sampleGuests = [
      Guest(
        uuid: holderUuid,
        name: 'Titular Demo',
        guestType: 'holder',
        phone: '2280000000',
        email: 'titular@demo.com',
        passes: 3,
        role: 'Docente',
        school: 'Servicio Demo',
        amountPaid: 100,
        raffleNumber: 11,
      ),
      Guest(
        uuid: uuid.v4(),
        parentUuid: holderUuid,
        name: 'Acompañante Demo 1',
        guestType: 'companion',
        phone: '2280000000',
        email: 'titular@demo.com',
        passes: 1,
        role: 'Docente',
        school: 'Servicio Demo',
        amountPaid: 100,
        raffleNumber: 12,
      ),
      Guest(
        uuid: uuid.v4(),
        parentUuid: holderUuid,
        name: 'Acompañante Demo 2',
        guestType: 'companion',
        phone: '2280000000',
        email: 'titular@demo.com',
        passes: 1,
        role: 'Docente',
        school: 'Servicio Demo',
        amountPaid: 100,
        raffleNumber: 13,
      ),
      Guest(
        uuid: uuid.v4(),
        name: 'Invitado Individual',
        guestType: 'holder',
        phone: '2281112233',
        passes: 1,
        role: 'Staff',
        school: 'Mesa de acceso',
        amountPaid: 0,
        raffleNumber: 20,
      ),
    ];
    for (final guest in sampleGuests) {
      await upsertGuest(guest);
    }
    await setSetting('sync_mode', SyncMode.localOnly.name);
  }

  Future<List<Guest>> guests({String query = ''}) async {
    final term = '%${query.trim()}%';
    final rows = query.trim().isEmpty
        ? await db.query('guests', orderBy: 'checked_in_at ASC, name ASC')
        : await db.query(
            'guests',
            where:
                'name LIKE ? OR phone LIKE ? OR email LIKE ? OR school LIKE ? OR role LIKE ?',
            whereArgs: [term, term, term, term, term],
            orderBy: 'checked_in_at ASC, name ASC',
          );
    return rows.map(Guest.fromMap).toList();
  }

  Future<Guest?> guestByUuid(String uuid) async {
    final rows = await db.query(
      'guests',
      where: 'uuid = ?',
      whereArgs: [uuid],
      limit: 1,
    );
    return rows.isEmpty ? null : Guest.fromMap(rows.first);
  }

  Future<List<Guest>> groupFor(Guest guest) async {
    final holderUuid = guest.parentUuid ?? guest.uuid;
    final rows = await db.query(
      'guests',
      where: 'uuid = ? OR parent_uuid = ?',
      whereArgs: [holderUuid, holderUuid],
      orderBy: 'parent_uuid IS NOT NULL, name ASC',
    );
    return rows.map(Guest.fromMap).toList();
  }

  Future<void> upsertGuest(Guest guest) async {
    await db.insert(
      'guests',
      guest.toMap(),
      conflictAlgorithm: ConflictAlgorithm.replace,
    );
  }

  Future<void> checkInGuest(Guest guest, String staffUser) async {
    if (guest.isCheckedIn) return;
    final now = DateTime.now();
    await db.transaction((txn) async {
      await txn.update(
        'guests',
        {
          'checked_in_at': now.toIso8601String(),
          'sync_status': 'pending',
        },
        where: 'uuid = ?',
        whereArgs: [guest.uuid],
      );
      await txn.insert(
        'checkin_events',
        CheckInEvent(
          id: null,
          guestUuid: guest.uuid,
          staffUser: staffUser,
          deviceId: deviceId,
          checkedInAt: now,
          syncStatus: 'pending',
        ).toMap(),
      );
    });
  }

  Future<({int total, int checked, int pendingSync, double paid})>
      summary() async {
    final total = Sqflite.firstIntValue(
          await db.rawQuery('SELECT COUNT(*) FROM guests'),
        ) ??
        0;
    final checked = Sqflite.firstIntValue(
          await db.rawQuery(
            'SELECT COUNT(*) FROM guests WHERE checked_in_at IS NOT NULL',
          ),
        ) ??
        0;
    final pendingSync = Sqflite.firstIntValue(
          await db.rawQuery(
            "SELECT COUNT(*) FROM checkin_events WHERE sync_status = 'pending'",
          ),
        ) ??
        0;
    final paidRow = await db.rawQuery('SELECT SUM(amount_paid) AS paid FROM guests');
    final paid = ((paidRow.first['paid'] as num?) ?? 0).toDouble();
    return (total: total, checked: checked, pendingSync: pendingSync, paid: paid);
  }

  Future<String?> setting(String key) async {
    final rows = await db.query(
      'settings',
      where: 'key = ?',
      whereArgs: [key],
      limit: 1,
    );
    return rows.isEmpty ? null : rows.first['value'] as String;
  }

  Future<void> setSetting(String key, String value) async {
    await db.insert(
      'settings',
      {'key': key, 'value': value},
      conflictAlgorithm: ConflictAlgorithm.replace,
    );
  }
}

class BiometricGate {
  BiometricGate(this.auth);

  final LocalAuthentication auth;

  Future<bool> canUseBiometrics() async {
    return await auth.canCheckBiometrics || await auth.isDeviceSupported();
  }

  Future<bool> authorize(String reason) async {
    if (!await canUseBiometrics()) return true;
    return auth.authenticate(
      localizedReason: reason,
      options: const AuthenticationOptions(
        biometricOnly: false,
        stickyAuth: true,
      ),
    );
  }
}

class HomeShell extends StatefulWidget {
  const HomeShell({super.key, required this.store});

  final LocalStore store;

  @override
  State<HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<HomeShell> {
  late final BiometricGate biometricGate = BiometricGate(LocalAuthentication());
  int index = 0;
  bool locked = true;
  bool biometricsAvailable = false;

  @override
  void initState() {
    super.initState();
    unawaited(_unlock());
  }

  Future<void> _unlock() async {
    biometricsAvailable = await biometricGate.canUseBiometrics();
    final ok = await biometricGate.authorize('Desbloquea CheckInQR para staff.');
    if (!mounted) return;
    setState(() => locked = !ok);
  }

  @override
  Widget build(BuildContext context) {
    if (locked) {
      return Scaffold(
        body: Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 360),
            child: Card(
              child: Padding(
                padding: const EdgeInsets.all(20),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    const Icon(Icons.lock_outline, size: 48),
                    const SizedBox(height: 12),
                    const Text(
                      'CheckInQR bloqueado',
                      style: TextStyle(fontSize: 22, fontWeight: FontWeight.w800),
                    ),
                    const SizedBox(height: 8),
                    Text(
                      biometricsAvailable
                          ? 'Usa huella, rostro, PIN o contraseña del dispositivo.'
                          : 'Este dispositivo no reporta biometría disponible.',
                      textAlign: TextAlign.center,
                    ),
                    const SizedBox(height: 16),
                    FilledButton.icon(
                      onPressed: _unlock,
                      icon: const Icon(Icons.fingerprint),
                      label: const Text('Desbloquear'),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      );
    }

    final pages = [
      DashboardPage(store: widget.store),
      GuestsPage(store: widget.store, biometricGate: biometricGate),
      CheckInPage(store: widget.store, biometricGate: biometricGate),
      SyncPage(store: widget.store),
    ];
    return Scaffold(
      appBar: AppBar(
        title: const Text('CheckInQR Mobile'),
        actions: [
          IconButton(
            tooltip: 'Bloquear',
            onPressed: () => setState(() => locked = true),
            icon: const Icon(Icons.lock),
          ),
        ],
      ),
      body: SafeArea(child: pages[index]),
      bottomNavigationBar: NavigationBar(
        selectedIndex: index,
        onDestinationSelected: (value) => setState(() => index = value),
        destinations: const [
          NavigationDestination(
            icon: Icon(Icons.dashboard_outlined),
            selectedIcon: Icon(Icons.dashboard),
            label: 'Panel',
          ),
          NavigationDestination(
            icon: Icon(Icons.people_alt_outlined),
            selectedIcon: Icon(Icons.people_alt),
            label: 'Invitados',
          ),
          NavigationDestination(
            icon: Icon(Icons.qr_code_scanner),
            label: 'Check-in',
          ),
          NavigationDestination(
            icon: Icon(Icons.sync),
            label: 'Sync',
          ),
        ],
      ),
    );
  }
}

class DashboardPage extends StatefulWidget {
  const DashboardPage({super.key, required this.store});

  final LocalStore store;

  @override
  State<DashboardPage> createState() => _DashboardPageState();
}

class _DashboardPageState extends State<DashboardPage> {
  late Future<({int total, int checked, int pendingSync, double paid})> future =
      widget.store.summary();

  @override
  Widget build(BuildContext context) {
    return RefreshIndicator(
      onRefresh: () async => setState(() => future = widget.store.summary()),
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          const Text(
            'Evento offline-first',
            style: TextStyle(fontSize: 28, fontWeight: FontWeight.w900),
          ),
          const SizedBox(height: 6),
          const Text(
            'La app opera con SQLite local. Docker, VPS, Google o un dispositivo principal son destinos de sincronización opcionales.',
          ),
          const SizedBox(height: 16),
          FutureBuilder(
            future: future,
            builder: (context, snapshot) {
              final data = snapshot.data;
              if (data == null) {
                return const Center(child: CircularProgressIndicator());
              }
              return GridView.count(
                shrinkWrap: true,
                crossAxisCount: MediaQuery.sizeOf(context).width > 640 ? 4 : 2,
                crossAxisSpacing: 12,
                mainAxisSpacing: 12,
                physics: const NeverScrollableScrollPhysics(),
                children: [
                  MetricCard(label: 'Registrados', value: '${data.total}'),
                  MetricCard(label: 'Ingresaron', value: '${data.checked}'),
                  MetricCard(label: 'Pend. sync', value: '${data.pendingSync}'),
                  MetricCard(
                    label: 'Caja',
                    value: '\$${data.paid.toStringAsFixed(2)}',
                  ),
                ],
              );
            },
          ),
          const SizedBox(height: 16),
          const Card(
            child: Padding(
              padding: EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    'Modos previstos',
                    style: TextStyle(fontSize: 18, fontWeight: FontWeight.w800),
                  ),
                  SizedBox(height: 8),
                  Text('1. Solo dispositivo: importar/exportar y operar local.'),
                  Text('2. Principal local: este equipo sirve datos por WiFi.'),
                  Text('3. Lector: se conecta al principal con QR de red.'),
                  Text('4. Docker/VPS/Google: sincronización opcional.'),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class MetricCard extends StatelessWidget {
  const MetricCard({super.key, required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Text(
              value,
              style: TextStyle(
                color: Theme.of(context).colorScheme.primary,
                fontSize: 26,
                fontWeight: FontWeight.w900,
              ),
            ),
            const SizedBox(height: 6),
            Text(label, textAlign: TextAlign.center),
          ],
        ),
      ),
    );
  }
}

class GuestsPage extends StatefulWidget {
  const GuestsPage({
    super.key,
    required this.store,
    required this.biometricGate,
  });

  final LocalStore store;
  final BiometricGate biometricGate;

  @override
  State<GuestsPage> createState() => _GuestsPageState();
}

class _GuestsPageState extends State<GuestsPage> {
  final searchController = TextEditingController();
  late Future<List<Guest>> guestsFuture = widget.store.guests();

  @override
  void dispose() {
    searchController.dispose();
    super.dispose();
  }

  void refresh() {
    setState(() {
      guestsFuture = widget.store.guests(query: searchController.text);
    });
  }

  Future<void> manualCheckIn(Guest guest) async {
    final ok = await widget.biometricGate.authorize(
      'Autoriza el check-in manual de ${guest.name}.',
    );
    if (!ok || !mounted) return;
    await widget.store.checkInGuest(guest, 'staff-local');
    refresh();
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text('Check-in manual registrado: ${guest.name}')),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 16, 16, 8),
          child: TextField(
            controller: searchController,
            decoration: InputDecoration(
              labelText: 'Buscar por nombre, telefono, correo, escuela o rol',
              prefixIcon: const Icon(Icons.search),
              suffixIcon: IconButton(
                onPressed: () {
                  searchController.clear();
                  refresh();
                },
                icon: const Icon(Icons.clear),
              ),
            ),
            onChanged: (_) => refresh(),
          ),
        ),
        Expanded(
          child: FutureBuilder<List<Guest>>(
            future: guestsFuture,
            builder: (context, snapshot) {
              final guests = snapshot.data;
              if (guests == null) {
                return const Center(child: CircularProgressIndicator());
              }
              return ListView.separated(
                padding: const EdgeInsets.all(16),
                itemCount: guests.length,
                separatorBuilder: (_, _) => const SizedBox(height: 10),
                itemBuilder: (context, index) {
                  final guest = guests[index];
                  return GuestTile(
                    guest: guest,
                    trailing: guest.isCheckedIn
                        ? const StatusPill(label: 'Ingreso')
                        : FilledButton.icon(
                            onPressed: () => manualCheckIn(guest),
                            icon: const Icon(Icons.fingerprint),
                            label: const Text('Manual'),
                          ),
                  );
                },
              );
            },
          ),
        ),
      ],
    );
  }
}

class CheckInPage extends StatefulWidget {
  const CheckInPage({
    super.key,
    required this.store,
    required this.biometricGate,
  });

  final LocalStore store;
  final BiometricGate biometricGate;

  @override
  State<CheckInPage> createState() => _CheckInPageState();
}

class _CheckInPageState extends State<CheckInPage> {
  final codeController = TextEditingController();
  List<Guest> group = [];
  String message = 'Escanea o pega un UUID/QR para buscar el pase.';
  bool scannerOpen = false;

  @override
  void dispose() {
    codeController.dispose();
    super.dispose();
  }

  String extractUuid(String raw) {
    final match = RegExp(
      r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}',
    ).firstMatch(raw);
    return match?.group(0) ?? raw.trim();
  }

  Future<void> lookup(String rawCode) async {
    final uuid = extractUuid(rawCode);
    final guest = await widget.store.guestByUuid(uuid);
    if (!mounted) return;
    if (guest == null) {
      setState(() {
        group = [];
        message = 'No encontre ese pase en la base local.';
      });
      return;
    }
    final members = await widget.store.groupFor(guest);
    setState(() {
      group = members;
      message = members.length > 1
          ? 'QR de grupo: marca solo a quienes llegaron.'
          : 'Pase individual encontrado.';
    });
  }

  Future<void> mark(Guest guest) async {
    final ok = await widget.biometricGate.authorize(
      'Autoriza el ingreso de ${guest.name}.',
    );
    if (!ok) return;
    await widget.store.checkInGuest(guest, 'staff-local');
    await lookup(guest.uuid);
  }

  @override
  Widget build(BuildContext context) {
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        Text(
          'Check-in',
          style: Theme.of(context).textTheme.headlineMedium?.copyWith(
                fontWeight: FontWeight.w900,
              ),
        ),
        const SizedBox(height: 8),
        Text(message),
        const SizedBox(height: 12),
        TextField(
          controller: codeController,
          decoration: InputDecoration(
            labelText: 'UUID o contenido del QR',
            suffixIcon: IconButton(
              tooltip: 'Buscar',
              onPressed: () => lookup(codeController.text),
              icon: const Icon(Icons.search),
            ),
          ),
          onSubmitted: lookup,
        ),
        const SizedBox(height: 12),
        FilledButton.icon(
          onPressed: () => setState(() => scannerOpen = !scannerOpen),
          icon: const Icon(Icons.qr_code_scanner),
          label: Text(scannerOpen ? 'Cerrar camara' : 'Abrir camara'),
        ),
        if (scannerOpen) ...[
          const SizedBox(height: 12),
          SizedBox(
            height: 280,
            child: ClipRRect(
              borderRadius: BorderRadius.circular(12),
              child: MobileScanner(
                onDetect: (capture) {
                  final value = capture.barcodes.firstOrNull?.rawValue;
                  if (value == null) return;
                  setState(() => scannerOpen = false);
                  codeController.text = value;
                  unawaited(lookup(value));
                },
              ),
            ),
          ),
        ],
        const SizedBox(height: 16),
        ...group.map(
          (guest) => Padding(
            padding: const EdgeInsets.only(bottom: 10),
            child: GuestTile(
              guest: guest,
              trailing: guest.isCheckedIn
                  ? const StatusPill(label: 'Ingreso')
                  : FilledButton(
                      onPressed: () => mark(guest),
                      child: const Text('Marcar llegada'),
                    ),
            ),
          ),
        ),
      ],
    );
  }
}

class SyncPage extends StatefulWidget {
  const SyncPage({super.key, required this.store});

  final LocalStore store;

  @override
  State<SyncPage> createState() => _SyncPageState();
}

class _SyncPageState extends State<SyncPage> {
  SyncMode mode = SyncMode.localOnly;
  final endpointController = TextEditingController();
  bool loaded = false;

  @override
  void initState() {
    super.initState();
    unawaited(load());
  }

  @override
  void dispose() {
    endpointController.dispose();
    super.dispose();
  }

  Future<void> load() async {
    final value = await widget.store.setting('sync_mode');
    final endpoint = await widget.store.setting('sync_endpoint');
    if (!mounted) return;
    setState(() {
      mode = SyncMode.values.firstWhere(
        (item) => item.name == value,
        orElse: () => SyncMode.localOnly,
      );
      endpointController.text = endpoint ?? '';
      loaded = true;
    });
  }

  Future<void> save() async {
    await widget.store.setSetting('sync_mode', mode.name);
    await widget.store.setSetting('sync_endpoint', endpointController.text);
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('Configuracion de sincronizacion guardada')),
    );
  }

  @override
  Widget build(BuildContext context) {
    if (!loaded) return const Center(child: CircularProgressIndicator());
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        const Text(
          'Sincronizacion',
          style: TextStyle(fontSize: 28, fontWeight: FontWeight.w900),
        ),
        const SizedBox(height: 8),
        const Text(
          'La app siempre trabaja primero con SQLite local. Esta pantalla prepara el destino opcional para compartir o respaldar datos.',
        ),
        const SizedBox(height: 16),
        Card(
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              children: [
                DropdownButtonFormField<SyncMode>(
                  initialValue: mode,
                  decoration: const InputDecoration(labelText: 'Modo'),
                  items: SyncMode.values
                      .map(
                        (item) => DropdownMenuItem(
                          value: item,
                          child: Text(item.label),
                        ),
                      )
                      .toList(),
                  onChanged: (value) {
                    if (value != null) setState(() => mode = value);
                  },
                ),
                const SizedBox(height: 12),
                TextField(
                  controller: endpointController,
                  decoration: const InputDecoration(
                    labelText: 'URL / IP / proyecto',
                    hintText: 'http://192.168.1.20:8080 o firebase project',
                  ),
                ),
                const SizedBox(height: 12),
                SizedBox(
                  width: double.infinity,
                  child: FilledButton.icon(
                    onPressed: save,
                    icon: const Icon(Icons.save),
                    label: const Text('Guardar configuracion'),
                  ),
                ),
              ],
            ),
          ),
        ),
        const SizedBox(height: 16),
        const Card(
          child: Padding(
            padding: EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  'Siguiente paso tecnico',
                  style: TextStyle(fontSize: 18, fontWeight: FontWeight.w800),
                ),
                SizedBox(height: 8),
                Text('• Principal local: levantar HTTP/WebSocket en Android.'),
                Text('• Lectores: cola local y reintentos de sincronizacion.'),
                Text('• Docker/VPS: API compatible con la cola local.'),
                Text('• Google: conector Firebase/Auth/Firestore opcional.'),
              ],
            ),
          ),
        ),
      ],
    );
  }
}

class GuestTile extends StatelessWidget {
  const GuestTile({super.key, required this.guest, required this.trailing});

  final Guest guest;
  final Widget trailing;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: ListTile(
        title: Text(
          guest.name,
          style: const TextStyle(fontWeight: FontWeight.w800),
        ),
        subtitle: Text(
          [
            guest.guestType == 'holder' ? 'Titular' : 'Acompanante',
            if (guest.role.isNotEmpty) guest.role,
            if (guest.school.isNotEmpty) guest.school,
            'Rifa #${guest.raffleNumber}',
            '\$${guest.amountPaid.toStringAsFixed(2)}',
          ].join(' · '),
        ),
        trailing: trailing,
      ),
    );
  }
}

class StatusPill extends StatelessWidget {
  const StatusPill({super.key, required this.label});

  final String label;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 7),
      decoration: BoxDecoration(
        color: Colors.green.withValues(alpha: 0.16),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: Colors.green.withValues(alpha: 0.35)),
      ),
      child: Text(
        label,
        style: const TextStyle(
          color: Color(0xFF86EFAC),
          fontWeight: FontWeight.w800,
        ),
      ),
    );
  }
}
