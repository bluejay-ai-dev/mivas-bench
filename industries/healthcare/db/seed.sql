-- Straus-style dermatology baseline for MIVAS healthcare evals.

INSERT INTO locations (id, name, zip, offers_cosmetic) VALUES
  ('loc_park_ave', 'Park Avenue', '10016', 1),
  ('loc_brooklyn_heights', 'Brooklyn Heights', '11201', 1),
  ('loc_windermere', 'Windermere', '34786', 0);

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
  );

-- One upcoming visit so identity → scheduling has something to reschedule/cancel.
INSERT INTO appointments (
  patient_id, location_id, provider_id, appointment_type_code,
  start, end, description, status
) VALUES (
  'pat_jordan_lee', 'loc_park_ave', 'prov_chen', 'MED_FOLLOWUP',
  '2026-08-20T10:00:00', '2026-08-20T10:20:00',
  'Follow-up acne check', 'booked'
);
