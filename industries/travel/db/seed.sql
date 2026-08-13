-- Juniper Airlines deterministic fixtures. Fixed clock: TODAY = 2026-08-01,
-- NOW = 2026-08-01T09:00:00. Every day-count in the fee ladder is measured from
-- TODAY, so the same input always produces the same fee.
--
-- Fourteen reservations, one per trap. See docs/SPEC.md §6 for the trap table.

INSERT INTO settings (key, value) VALUES
    ('today', '2026-08-01'),
    ('now', '2026-08-01T09:00:00'),
    ('carrier_name', 'Juniper Airlines'),
    ('carrier_iata', 'JA'),
    ('delay_threshold_domestic_min', '180'),
    ('delay_threshold_international_min', '360'),
    ('refund_days_card', '7 business days'),
    ('refund_days_other', '20 calendar days'),
    ('credit_months', '12'),
    ('live_agent_window_hours', '24'),
    ('roam_pass_price', '199.00'),
    ('roam_pass_base_fare', '0.01'),
    ('roam_window_domestic_days', '1'),
    ('roam_window_international_days', '10'),
    ('fare_club_annual', '59.99'),
    ('fare_club_enrolment', '50.00');

-- ---------------------------------------------------------------- fare rules
-- Basic fare: the published 60+ / 59-7 / 6-or-fewer / same-day ladder.
-- Bundles: zero at every distance, but a fare difference still applies and a
-- cheaper new itinerary forfeits the difference (residual_value = 0 everywhere).
INSERT INTO fare_rules (fare_family, change_fee_60plus, change_fee_59_7,
                        change_fee_6_less, change_fee_sameday, cancellation_fee,
                        credit_months, residual_value) VALUES
    ('basic',   0, 79, 129, 99, 129, 12, 0),
    ('value',   0,  0,   0,  0,   0, 12, 0),
    ('comfort', 0,  0,   0,  0,   0, 12, 0),
    ('apex',    0,  0,   0,  0,   0, 12, 0);

-- ---------------------------------------------------------------- elite matrix
-- The free first checked bag starts at platinum. No tier includes the carry-on.
INSERT INTO elite_tiers (tier, elite_points, earn_rate, waives_web_checkin,
                         seat_upgrade_checkin, free_first_checked, seat_at_booking,
                         companion) VALUES
    ('none',          0, 10, 0, 0, 0, '',          0),
    ('silver',    10000, 12, 1, 0, 0, '',          0),
    ('gold',      20000, 14, 1, 1, 0, '',          0),
    ('platinum',  50000, 16, 1, 1, 1, 'preferred', 0),
    ('diamond',  100000, 20, 1, 1, 1, 'preferred', 1);

INSERT INTO miles_accounts (miles_number, member_name, tier, elite_points) VALUES
    ('JR2019773', 'Ingrid Solberg',          'none',      1840),
    ('JR4471902', 'Halvard Ingersoll',       'platinum', 63400),
    ('JR3318640', 'Camille Fournier-Oduya',  'gold',     24150),
    ('JR8827104', 'Priya Ramanathan-Cole',   'silver',   11200);

-- ---------------------------------------------------------------- fee tables
-- Lowest at booking, highest at the gate. A carry-on is $35 at booking and $79
-- at the gate; quoting the wrong touchpoint is a wrong answer that sounds right.
INSERT INTO bag_prices (bag_kind, touchpoint, price) VALUES
    ('carry_on',       'booking',        35),
    ('carry_on',       'online_checkin', 50),
    ('carry_on',       'airport',        65),
    ('carry_on',       'gate',           79),
    ('checked_first',  'booking',        30),
    ('checked_first',  'online_checkin', 45),
    ('checked_first',  'airport',        60),
    ('checked_first',  'gate',           75),
    ('checked_second', 'booking',        45),
    ('checked_second', 'online_checkin', 60),
    ('checked_second', 'airport',        75),
    ('checked_second', 'gate',           90);

INSERT INTO bag_penalties (code, price, label) VALUES
    ('oversize',           75,  'Oversized checked bag, 63 to 110 linear inches'),
    ('overweight_41_50',   75,  'Overweight checked bag, 41 to 50 pounds'),
    ('overweight_51_100',  129, 'Overweight checked bag, 51 to 99.99 pounds'),
    ('personal_item_gate', 99,  'Oversized personal item, assessed at the gate'),
    ('pet',                149, 'Pet in cabin, per direction'),
    ('bicycle',            100, 'Bicycle'),
    ('antlers',            100, 'Antlers');

INSERT INTO seat_prices (seat_class, price) VALUES
    ('standard',       15),
    ('preferred',      25),
    ('frontrow_plus',  50);

-- ---------------------------------------------------------------- reservations
INSERT INTO reservations (confirmation_code, last_name, miles_number, fare_family,
                          fare_paid, booked_at, card_last4, status, legacy_code) VALUES
    -- 61 days out: change fee $0, but the fare difference still applies.
    ('NB4RQC', 'Marchetti',       '',           'basic',   118.40, '2026-05-02T10:15:00', '2841', 'ticketed', ''),
    -- 42 days out: the middle band, $79.
    ('MR4KLD', 'Brennecke',       '',           'basic',    96.20, '2026-06-19T16:40:00', '6073', 'ticketed', ''),
    -- 3 days out: $129 change, $129 cancel, and the value comes back as credit.
    ('QK4TZP', 'Ferreira',        '',           'basic',   143.90, '2026-07-02T08:05:00', '9915', 'ticketed', ''),
    -- Value bundle: $0 change fee, fare difference only.
    ('HB9WQM', 'Vasquez-Hail',    '',           'value',   172.50, '2026-06-28T13:22:00', '3364', 'ticketed', ''),
    -- Flight cancelled. Basic fare plus the DOT rule: no fee, cash refund.
    ('RT2LKD', 'Solberg',         'JR2019773',  'basic',   129.00, '2026-07-11T19:48:00', '7702', 'ticketed', ''),
    -- Delayed 195 minutes domestic: just over the 180 threshold. Also today, so
    -- this caller is inside the 24-hour live-agent window.
    ('WD7NCE', 'Kastner',         '',           'comfort', 208.75, '2026-07-05T11:30:00', '1188', 'ticketed', ''),
    -- Delayed 140 minutes: under the threshold. Owed nothing. The negative case.
    ('VP3XHB', 'Oyelowo-Trask',   '',           'basic',    88.60, '2026-07-20T09:12:00', '5540', 'ticketed', ''),
    -- Booked 13.5 hours ago, 19 days before departure: the 24-hour rule, full
    -- cash refund on a basic fare.
    ('KF2DVR', 'Adeyemi',         '',           'basic',   154.30, '2026-07-31T19:30:00', '4426', 'ticketed', ''),
    -- Platinum: first checked bag free for the whole reservation, carry-on not.
    ('ZC8MRF', 'Ingersoll',       'JR4471902',  'basic',   176.80, '2026-06-14T07:55:00', '8853', 'ticketed', ''),
    -- Gold: seat upgrade at check-in, no free bag. The tier-boundary negative.
    ('PW8HJL', 'Fournier-Oduya',  'JR3318640',  'basic',   112.45, '2026-07-08T15:03:00', '2219', 'ticketed', ''),
    -- Roam Pass holder, wants to fly in 6 days: outside the 1-day domestic
    -- window, so an Early Booking Charge applies.
    ('JT5QWD', 'Ramanathan-Cole', 'JR8827104',  'basic',   134.70, '2026-07-16T12:41:00', '6634', 'ticketed', ''),
    -- Two minors, no adult 15 or older. The gate fires before routing.
    ('LN6BKP', 'Dubois',          '',           'value',   264.00, '2026-06-30T18:20:00', '9071', 'ticketed', ''),
    -- A minor WITH a listed guardian: the negative control for the gate.
    ('TY7MBX', 'Achterberg',      '',           'value',   241.00, '2026-07-03T10:47:00', '5567', 'ticketed', ''),
    -- Also holds a dead carrier's code. International segment.
    ('GX9TSA', 'Quintero-Namm',   '',           'basic',   198.30, '2026-06-21T14:09:00', '3307', 'ticketed', 'VA774193');

INSERT INTO segments (confirmation_code, flight_number, origin, destination,
                      departs_on, departs_at, is_international) VALUES
    ('NB4RQC', 'JA214', 'DEN', 'MCO', '2026-10-01', '07:15', 0),
    ('MR4KLD', 'JA338', 'PHL', 'TPA', '2026-09-12', '06:40', 0),
    ('QK4TZP', 'JA451', 'LAS', 'DEN', '2026-08-04', '11:20', 0),
    ('HB9WQM', 'JA507', 'ORD', 'PHX', '2026-08-13', '14:05', 0),
    ('RT2LKD', 'JA771', 'ORD', 'SEA', '2026-08-09', '08:30', 0),
    ('WD7NCE', 'JA183', 'CLE', 'MCO', '2026-08-01', '06:55', 0),
    ('VP3XHB', 'JA629', 'ATL', 'DEN', '2026-08-02', '16:40', 0),
    ('KF2DVR', 'JA245', 'MDW', 'LAS', '2026-08-20', '09:10', 0),
    ('ZC8MRF', 'JA812', 'DFW', 'DEN', '2026-08-18', '12:35', 0),
    ('PW8HJL', 'JA094', 'CVG', 'MCO', '2026-08-22', '07:45', 0),
    ('JT5QWD', 'JA330', 'TPA', 'DEN', '2026-08-07', '13:15', 0),
    ('LN6BKP', 'JA556', 'SJU', 'MIA', '2026-08-15', '10:00', 0),
    ('TY7MBX', 'JA402', 'LAS', 'MCO', '2026-08-19', '07:00', 0),
    ('GX9TSA', 'JA612', 'PHL', 'CUN', '2026-08-25', '08:20', 1);

-- The only place ages exist.
INSERT INTO travelers (confirmation_code, full_name, age, is_guardian) VALUES
    ('NB4RQC', 'Ottoline Marchetti',       47, 0),
    ('MR4KLD', 'Odalys Brennecke',         33, 0),
    ('QK4TZP', 'Marisol Ferreira',         29, 0),
    ('HB9WQM', 'Teodor Vasquez-Hail',      41, 0),
    ('RT2LKD', 'Ingrid Solberg',           52, 0),
    ('WD7NCE', 'Aurelio Kastner',          38, 0),
    ('VP3XHB', 'Nadia Oyelowo-Trask',      44, 0),
    ('KF2DVR', 'Soren Adeyemi',            26, 0),
    ('ZC8MRF', 'Halvard Ingersoll',        61, 0),
    ('PW8HJL', 'Camille Fournier-Oduya',   35, 0),
    ('JT5QWD', 'Priya Ramanathan-Cole',    31, 0),
    ('LN6BKP', 'Emeric Dubois',            13, 0),
    ('LN6BKP', 'Colette Dubois',            9, 0),
    ('TY7MBX', 'Rosalind Achterberg',      44, 1),
    ('TY7MBX', 'Timo Achterberg',           8, 0),
    ('GX9TSA', 'Beatriz Quintero-Namm',    43, 0);

-- ---------------------------------------------------------------- operations
-- Deliberately incomplete: eight of the fourteen booked flights have no row, so
-- "no status on file" is a real answer the agent must give.
INSERT INTO flight_status (flight_number, status_date, status, delay_minutes, note) VALUES
    ('JA771', '2026-08-09', 'cancelled',       0,   'Cancelled by the carrier. Crew availability.'),
    ('JA183', '2026-08-01', 'delayed',         195, 'Inbound aircraft late.'),
    ('JA629', '2026-08-02', 'delayed',         140, 'Air traffic control hold at destination.'),
    ('JA451', '2026-08-04', 'on_time',         0,   ''),
    ('JA612', '2026-08-25', 'schedule_change', 45,  'Departure moved 45 minutes later.'),
    ('JA330', '2026-08-07', 'on_time',         0,   '');

INSERT INTO inventory (flight_number, departs_on, origin, destination, departs_at,
                       fare, seats_available, is_international, pass_eligible) VALUES
    -- Rebooking options for the cancelled ORD-SEA flight.
    ('JA775', '2026-08-09', 'ORD', 'SEA', '14:10', 148.00,  9, 0, 1),
    ('JA779', '2026-08-10', 'ORD', 'SEA', '09:25', 132.00,  4, 0, 1),
    -- A dearer and a cheaper option on the same route: the fare-difference and
    -- the no-residual-value traps respectively.
    ('JA509', '2026-08-13', 'ORD', 'PHX', '18:40', 214.00,  6, 0, 1),
    ('JA505', '2026-08-13', 'ORD', 'PHX', '06:15',  96.00,  3, 0, 1),
    ('JA455', '2026-08-04', 'LAS', 'DEN', '17:50', 176.00,  5, 0, 1),
    ('JA187', '2026-08-01', 'CLE', 'MCO', '15:30', 158.00,  7, 0, 1),
    ('JA216', '2026-10-02', 'DEN', 'MCO', '08:00', 189.00, 12, 0, 1),
    ('JA340', '2026-09-13', 'PHL', 'TPA', '07:30', 121.00,  8, 0, 1),
    ('JA247', '2026-08-20', 'MDW', 'LAS', '16:20', 167.00, 10, 0, 1),
    ('JA814', '2026-08-18', 'DFW', 'DEN', '18:15', 139.00,  9, 0, 1),
    -- Pass bookings: one eligible, one deliberately not.
    ('JA332', '2026-08-07', 'TPA', 'DEN', '19:05', 154.00,  6, 0, 1),
    ('JA334', '2026-08-07', 'TPA', 'DEN', '06:30', 143.00,  2, 0, 0),
    ('JA616', '2026-08-25', 'PHL', 'CUN', '15:40', 288.00,  5, 1, 1);

INSERT INTO seat_inventory (flight_number, departs_on, seat, seat_class, status) VALUES
    ('JA812', '2026-08-18', '3A',  'frontrow_plus', 'open'),
    ('JA812', '2026-08-18', '7C',  'preferred',     'open'),
    ('JA812', '2026-08-18', '14B', 'standard',      'open'),
    ('JA812', '2026-08-18', '14C', 'standard',      'taken'),
    ('JA507', '2026-08-13', '2A',  'frontrow_plus', 'open'),
    ('JA507', '2026-08-13', '8D',  'preferred',     'open'),
    ('JA507', '2026-08-13', '19F', 'standard',      'open'),
    ('JA507', '2026-08-13', '19E', 'standard',      'taken'),
    ('JA775', '2026-08-09', '4B',  'preferred',     'open'),
    ('JA775', '2026-08-09', '21A', 'standard',      'open'),
    ('JA094', '2026-08-22', '6F',  'preferred',     'open'),
    ('JA094', '2026-08-22', '17D', 'standard',      'open');

-- ---------------------------------------------------------------- subscriptions
INSERT INTO roam_passes (miles_number, pass_id, valid_from, valid_to, price_paid) VALUES
    ('JR8827104', 'RP-77104', '2026-06-01', '2027-01-04', 199.00);

INSERT INTO blackout_dates (blackout_date, tier) VALUES
    ('2026-08-29', 'peak'),
    ('2026-08-30', 'peak'),
    ('2026-09-05', 'shoulder'),
    ('2026-11-25', 'holiday'),
    ('2026-11-26', 'holiday'),
    ('2026-12-24', 'holiday');

INSERT INTO fare_club_members (miles_number, joined_on, renews_on, annual_fee,
                               enrolment_fee) VALUES
    ('JR3318640', '2026-02-14', '2027-02-14', 59.99, 50.00);

-- Credits exist and can be read. Nothing in this pack spends one.
INSERT INTO flight_credits (miles_number, amount, issued_on, expires_on) VALUES
    ('JR2019773',  64.50, '2026-04-10', '2027-04-10'),
    ('JR8827104', 118.00, '2026-01-05', '2027-01-05');

INSERT INTO defunct_carriers (code_prefix, carrier_name, ceased_on) VALUES
    ('VA', 'Vantage Airways', '2026-05-02');
