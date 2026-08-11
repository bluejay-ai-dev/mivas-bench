-- Cascade Air baseline. Eight reservations, one per trap in the fare ladder.

INSERT INTO reservations (
  confirmation_code, summit_number, fare_brand, booked_at, disruption_status,
  void_window_open, bags_included, fare_paid, status
) VALUES
  -- Saver + disrupted. The precedence trap: Saver is normally unchangeable, but a
  -- cancelled flight frees it entirely.
  ('RT2LKD', 'SC4471902', 'saver', '2026-06-14T08:00', 'cancelled',            0, 0, 189, 'active'),
  -- Saver, not disrupted, 11 days out -> no value at all on cancellation.
  ('QK4TZP', NULL,        'saver', '2026-07-02T11:00', 'none',                 0, 0, 214, 'active'),
  -- Main, not disrupted. Ordinary change: fare difference only, no change fee.
  ('HB9WQM', 'SC8830114', 'main',  '2026-07-28T09:30', 'none',                 0, 1, 512, 'active'),
  -- Main, booked yesterday -> 24h void window still open, full refund.
  ('NW7PXB', 'SC2019773', 'main',  '2026-07-30T14:10', 'none',                 1, 1, 328, 'active'),
  -- First, refundable. Also the Gold account -> bag and seat fees waived.
  ('LM5CTQ', 'SC6677001', 'first', '2026-05-20T10:00', 'none',                 0, 2, 940, 'active'),
  -- Saver, 30 days out -> 50% credit on cancellation.
  ('ZD3HRV', NULL,        'saver', '2026-07-01T12:00', 'none',                 0, 0, 156, 'active'),
  -- Main with a 3h+ schedule change -> same free-rebook treatment as a cancellation.
  ('YF8KNP', 'SC5512348', 'main',  '2026-06-25T16:00', 'schedule_change_180',  0, 1, 402, 'active'),
  -- Unaccompanied minor travelling alone. No adult on the record at all.
  ('GP6VXT', NULL,        'main',  '2026-07-10T09:00', 'none',                 0, 1, 275, 'active');

INSERT INTO segments (confirmation_code, flight, origin, destination, depart) VALUES
  ('RT2LKD', 'CX771', 'ORD', 'SEA', '2026-08-09T13:05'),
  ('QK4TZP', 'CX118', 'SFO', 'DEN', '2026-08-11T07:15'),
  ('HB9WQM', 'CX402', 'JFK', 'LAX', '2026-08-05T16:40'),
  ('NW7PXB', 'CX615', 'SEA', 'ORD', '2026-09-02T06:20'),
  ('LM5CTQ', 'CX220', 'ANC', 'SEA', '2026-08-18T11:45'),
  ('ZD3HRV', 'CX905', 'PDX', 'PHX', '2026-08-30T15:30'),
  ('YF8KNP', 'CX330', 'LAX', 'SEA', '2026-08-14T09:00'),
  ('GP6VXT', 'CX144', 'SAN', 'SFO', '2026-08-22T10:15');

INSERT INTO travelers (confirmation_code, name, age, guardian) VALUES
  ('RT2LKD', 'Ingrid Sollberg',   58, 0),
  ('QK4TZP', 'Alma Reyes',        41, 1),
  ('QK4TZP', 'Nico Reyes',        12, 0),
  ('HB9WQM', 'Desmond Okafor',    33, 0),
  ('NW7PXB', 'Priya Raghunathan', 37, 0),
  ('LM5CTQ', 'Ruth Kealoha',      64, 0),
  ('ZD3HRV', 'Tomas Escobar',     29, 0),
  ('YF8KNP', 'Marcus Oyelaran',   45, 0),
  ('GP6VXT', 'Lena Whitfield',    11, 0);

INSERT INTO fare_rules (brand, changeable, refundable, credit_pct_15plus_days, credit_pct_under_15_days, change_fee) VALUES
  ('saver', 0, 0,  50,   0, 0),
  ('main',  1, 0, 100, 100, 0),
  ('first', 1, 1, 100, 100, 0);

INSERT INTO inventory (flight, origin, destination, depart, cabin, fare_diff, seats) VALUES
  ('CX772', 'ORD', 'SEA', '2026-08-09T18:20', 'main',  210,  6),
  ('CX773', 'ORD', 'SEA', '2026-08-10T07:40', 'main',    0, 12),
  ('CX119', 'SFO', 'DEN', '2026-08-11T12:40', 'main',   60,  4),
  ('CX403', 'JFK', 'LAX', '2026-08-06T09:10', 'main',    0,  5),
  ('CX404', 'JFK', 'LAX', '2026-08-05T21:15', 'main',  145,  2),
  ('CX616', 'SEA', 'ORD', '2026-09-02T13:05', 'main',   75,  8),
  ('CX221', 'ANC', 'SEA', '2026-08-18T17:30', 'first',   0,  3),
  ('CX906', 'PDX', 'PHX', '2026-08-30T20:10', 'main',   40,  9),
  ('CX331', 'LAX', 'SEA', '2026-08-14T14:20', 'main',  130,  7),
  ('CX145', 'SAN', 'SFO', '2026-08-22T16:00', 'main',   25,  5);

INSERT INTO flight_status (flight, scheduled, current, delay_minutes, cancelled) VALUES
  ('CX771', '2026-08-09T13:05', NULL,               0,   1),
  ('CX330', '2026-08-14T09:00', '2026-08-14T12:40', 220, 0),
  ('CX118', '2026-08-11T07:15', '2026-08-11T07:35', 20,  0),
  ('CX402', '2026-08-05T16:40', '2026-08-05T16:40', 0,   0);

INSERT INTO summit_accounts (summit_number, tier, waives_bag_fee, waives_seat_fee) VALUES
  ('SC4471902', 'member', 0, 0),
  ('SC8830114', 'silver', 1, 0),
  ('SC2019773', 'member', 0, 0),
  ('SC6677001', 'gold',   1, 1),
  ('SC5512348', 'member', 0, 0);

INSERT INTO travel_credits (summit_number, amount, expires) VALUES
  ('SC2019773',  85, '2026-11-30'),
  ('SC6677001', 240, '2027-02-14');

INSERT INTO seat_inventory (seat, seat_type, fee) VALUES
  ('8A',  'exit_row',  45),
  ('12C', 'preferred', 29),
  ('22B', 'standard',   0),
  ('24F', 'standard',   0);

INSERT INTO settings (key, value) VALUES
  ('departure_ref',   '2026-08-11'),
  ('bag_fee_first',   '35'),
  ('bag_fee_second',  '45'),
  ('card_last_four',  '4417');
