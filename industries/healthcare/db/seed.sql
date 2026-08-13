-- Straus-style dermatology baseline for MIVAS healthcare evals.

INSERT INTO locations (
  id, name, zip, offers_cosmetic, address, floor, suite, hours, services, transit, parking
) VALUES
  (
    'loc_park_ave', 'Park Avenue', '10016', 1,
    '386 Park Avenue South, New York, NY 10016', '4th floor', 'Suite 410',
    'Mon-Fri 8am-5pm, Sat 9am-1pm', 'medical, surgical, cosmetic, allergy',
    'One block from the 6 train at 33rd Street', 'Garage next door on East 27th'
  ),
  (
    'loc_brooklyn_heights', 'Brooklyn Heights', '11201', 1,
    '142 Montague Street, Brooklyn, NY 11201', '2nd floor', 'Suite 2B',
    'Mon-Fri 8am-5pm', 'medical, surgical, cosmetic, allergy',
    'Two blocks from Borough Hall', 'Street parking; garage on Clinton Street'
  ),
  (
    'loc_windermere', 'Windermere', '34786', 0,
    '7600 Conroy Windermere Road, Windermere, FL 34786', 'Ground floor', 'Suite 100',
    'Mon-Fri 8am-5pm', 'medical, surgical, allergy',
    'No transit nearby', 'Free surface lot'
  );

INSERT INTO providers (id, name, credentials, location_id) VALUES
  ('prov_chen', 'Dr. Amy Chen', 'MD, FAAD', 'loc_park_ave'),
  ('prov_ruiz', 'Dr. Luis Ruiz', 'MD', 'loc_brooklyn_heights'),
  ('prov_patel', 'Dr. Neha Patel', 'DO', 'loc_windermere');

INSERT INTO patients (
  id, first_name, last_name, dob, zip, phone_e164,
  home_office_id, language, balance_cents, carrier, member_id, plan_name
) VALUES
  (
    'pat_jordan_lee', 'Jordan', 'Lee', '1990-04-12', '10016', '+12125550100',
    'loc_park_ave', 'en', 12500, 'Aetna', 'W123456789', 'Open Access Elect Choice'
  ),
  (
    'pat_sam_nguyen', 'Sam', 'Nguyen', '1985-11-03', '11201', '+17185550122',
    'loc_brooklyn_heights', 'en', 0, 'UnitedHealthcare', 'U987654321', 'Choice Plus'
  ),
  -- $480 balance: the only callers CareCredit is offered to (financing starts at $250).
  (
    'pat_maria_alvarez', 'Maria', 'Alvarez', '1972-06-30', '10016', '+12125550133',
    'loc_park_ave', 'en', 48000, 'Cigna', 'C445566778', 'Open Access Plus'
  ),
  -- Carrier absent from contracting info → check_plan_accepted must_not_assert.
  (
    'pat_alice_romano', 'Alice', 'Romano', '1995-09-08', '34786', '+14075550155',
    'loc_windermere', 'en', 32000, 'Oscar Health', 'O778899001', 'Circle EPO'
  ),
  -- Minor: a guardian verifying on the child's name + DOB is an allowed proxy.
  (
    'pat_leo_park', 'Leo', 'Park', '2016-03-22', '11201', '+17185550166',
    'loc_brooklyn_heights', 'en', 0, 'Aetna', 'W998877665', 'Open Access Elect Choice'
  );

-- Upcoming visits so identity → scheduling has something to reschedule/cancel.
-- Relative to tool_server.TODAY (2026-08-19T12:00): id 1 is inside the 24 h
-- medical window ($50), id 2 inside the 72 h cosmetic window ($125 + forfeited
-- deposit), id 3 far outside any window (free to cancel).
INSERT INTO appointments (
  patient_id, location_id, provider_id, appointment_type_code,
  start, end, description, status
) VALUES
  (
    'pat_jordan_lee', 'loc_park_ave', 'prov_chen', 'MED_FOLLOWUP',
    '2026-08-20T10:00:00', '2026-08-20T10:20:00',
    'Follow-up acne check', 'booked'
  ),
  (
    'pat_maria_alvarez', 'loc_park_ave', 'prov_chen', 'COS_CONSULT',
    '2026-08-21T15:00:00', '2026-08-21T15:30:00',
    'Botox consult', 'booked'
  ),
  (
    'pat_alice_romano', 'loc_windermere', 'prov_patel', 'MED_FOLLOWUP',
    '2026-09-15T09:00:00', '2026-09-15T09:20:00',
    'Eczema follow-up', 'booked'
  );
