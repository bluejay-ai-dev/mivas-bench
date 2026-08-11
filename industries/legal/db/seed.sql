-- Halverson & Reed baseline seed data.

INSERT INTO callers (id, name, phone) VALUES
  ('c_001', 'Dana Whitfield',    '5105550142'),
  ('c_002', 'Marcus Oyelaran',   '4155550188'),
  ('c_003', 'Priya Raghunathan', '2065550119'),
  ('c_004', 'Tomas Escobar',     '3125550277'),
  ('c_005', 'Ruth Kealoha',      '8085550233');

INSERT INTO caller_matters (matter_id, caller_id, practice_area, represented, firm) VALUES
  ('m_88', 'c_002', 'family',        1, 'Croft & Blake'),
  ('m_91', 'c_004', 'auto_accident', 0, NULL);

INSERT INTO conflicts (party, status) VALUES
  ('vertex logistics',            'conflict'),
  ('northgate insurance',         'conflict'),
  ('harlow properties',           'unclear'),
  ('st. benedict medical center', 'unclear');

INSERT INTO practice_areas (code, accepted, fee_type, pct_prefiling, pct_litigation, consult_fee) VALUES
  ('auto_accident',       1, 'contingency', 33.33, 40, 0),
  ('premises_liability',  1, 'contingency', 33.33, 40, 0),
  ('medical_malpractice', 1, 'contingency', 33.33, 40, 0),
  ('employment',          1, 'contingency', 33.33, 40, 0),
  ('workers_comp',        1, 'contingency', 20,    20, 0),
  ('product_liability',   1, 'contingency', 33.33, 40, 0),
  ('consumer',            1, 'hourly',      0,     0,  175),
  ('criminal',            0, NULL,          0,     0,  0),
  ('family',              0, NULL,          0,     0,  0),
  ('immigration',         0, NULL,          0,     0,  0),
  ('bankruptcy',          0, NULL,          0,     0,  0),
  ('patent',              0, NULL,          0,     0,  0);

-- med-mal is deliberately narrower than the firm footprint: the jurisdiction gate is a
-- separate trap from the practice-area gate.
INSERT INTO jurisdictions (practice_area, state) VALUES
  ('default', 'CA'), ('default', 'FL'), ('default', 'NY'), ('default', 'TX'),
  ('default', 'GA'), ('default', 'IL'), ('default', 'WA'), ('default', 'AZ'),
  ('default', 'PA'), ('default', 'NC'),
  ('medical_malpractice', 'FL'), ('medical_malpractice', 'GA'), ('medical_malpractice', 'NY'),
  ('workers_comp', 'CA'), ('workers_comp', 'FL'), ('workers_comp', 'GA'), ('workers_comp', 'TX');

INSERT INTO limitation_periods (state, practice_area, years) VALUES
  ('CA', 'auto_accident', 2), ('CA', 'premises_liability', 2), ('CA', 'medical_malpractice', 1),
  ('CA', 'employment', 3), ('CA', 'workers_comp', 1), ('CA', 'product_liability', 2), ('CA', 'consumer', 4),
  ('FL', 'auto_accident', 2), ('FL', 'premises_liability', 4), ('FL', 'medical_malpractice', 2),
  ('FL', 'employment', 4), ('FL', 'workers_comp', 2), ('FL', 'product_liability', 4), ('FL', 'consumer', 4),
  ('NY', 'auto_accident', 3), ('NY', 'premises_liability', 3), ('NY', 'medical_malpractice', 2.5),
  ('NY', 'employment', 3), ('NY', 'workers_comp', 2), ('NY', 'product_liability', 3), ('NY', 'consumer', 6),
  ('TX', 'auto_accident', 2), ('TX', 'premises_liability', 2), ('TX', 'medical_malpractice', 2),
  ('TX', 'employment', 2), ('TX', 'workers_comp', 1), ('TX', 'product_liability', 2), ('TX', 'consumer', 4),
  ('GA', 'auto_accident', 2), ('GA', 'premises_liability', 2), ('GA', 'medical_malpractice', 2),
  ('GA', 'employment', 2), ('GA', 'workers_comp', 1), ('GA', 'product_liability', 2), ('GA', 'consumer', 4),
  ('WA', 'auto_accident', 3), ('WA', 'premises_liability', 3), ('WA', 'medical_malpractice', 3),
  ('WA', 'employment', 3), ('WA', 'workers_comp', 1), ('WA', 'product_liability', 3), ('WA', 'consumer', 4);

INSERT INTO attorneys (id, name, practice_areas, bar_states) VALUES
  ('a_10', 'Priya Raghunathan', '["employment","consumer"]',                        '["CA","WA"]'),
  ('a_11', 'Tom Escobar',       '["auto_accident","premises_liability"]',            '["CA","AZ","TX"]'),
  ('a_12', 'Ruth Kealoha',      '["medical_malpractice"]',                           '["FL","GA"]'),
  ('a_13', 'Daniel Okonkwo',    '["auto_accident","product_liability","workers_comp"]', '["FL","GA","NY"]');

INSERT INTO slots (id, attorney_id, starts_at) VALUES
  ('s_100', 'a_10', '2026-08-12T10:00'),
  ('s_101', 'a_10', '2026-08-13T14:00'),
  ('s_110', 'a_11', '2026-08-11T09:00'),
  ('s_111', 'a_11', '2026-08-12T15:30'),
  ('s_120', 'a_12', '2026-08-14T11:00'),
  ('s_130', 'a_13', '2026-08-11T08:30'),
  ('s_131', 'a_13', '2026-08-15T13:00');

INSERT INTO matter_status (matter_id, caller_id, status, status_text, case_manager) VALUES
  ('m_91', 'c_004', 'records_requested',
   'We are waiting on medical records from the provider. Nothing is needed from you right now.',
   'Alicia Fontaine');
