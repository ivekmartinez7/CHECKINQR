import 'package:checkinqr_mobile/main.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('Guest serializes local check-in fields', () {
    final guest = Guest(
      uuid: 'guest-1',
      name: 'Invitada Demo',
      guestType: 'holder',
      passes: 1,
      amountPaid: 100,
      raffleNumber: 7,
    );

    final map = guest.toMap();
    final copy = Guest.fromMap(map);

    expect(copy.uuid, 'guest-1');
    expect(copy.name, 'Invitada Demo');
    expect(copy.isCheckedIn, isFalse);
    expect(copy.amountPaid, 100);
  });
}
