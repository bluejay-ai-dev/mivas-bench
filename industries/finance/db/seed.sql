-- Copperline Credit Union — deterministic fixtures. Fixed clock: TODAY = 2026-08-01.
-- Every persona in docs/ONEPAGER.md has a row; every policy trap is reachable from here.

INSERT INTO branches (id, name, address, hours, services) VALUES
('br_averton',  'Averton (Headquarters)', '400 Copperline Way, Averton, PA', 'Mon-Fri 8:30am-5:30pm, Sat 9am-1pm', 'full service, drive-up, notary, safe deposit'),
('br_granford', 'Granford',               '12 Mill Race Road, Granford, PA', 'Mon-Fri 9am-5pm, Sat 9am-noon',     'full service, drive-up'),
('br_marklin',  'Marklin Crossing',       '208 Furnace Street, Marklin Crossing, PA', 'Mon-Fri 9am-5pm',          'full service, notary'),
('br_harrow',   'Harrow Mills',           '77 Weaver Avenue, Harrow Mills, PA', 'Mon-Fri 9am-5pm, Sat 9am-noon',  'full service, drive-up, notary'),
('br_danbrook', 'Danbrook',               '1500 Keystone Pike, Danbrook, PA', 'Mon-Fri 9am-6pm, Sat 9am-1pm',     'full service, drive-up'),
('br_pell',     'Pell Creek',             '31 Canal Street, Pell Creek, PA',  'Mon-Fri 9am-5pm',                  'full service');

INSERT INTO fees (code, label, amount_text, conditions) VALUES
('courtesy_pay',            'Courtesy Pay (overdraft paid)',       '$33.00 per item',  'Maximum 3 combined NSF and Courtesy Pay fees per calendar day.'),
('nsf',                     'Non-sufficient funds (returned item)','$33.00 per item',  'Maximum 3 combined NSF and Courtesy Pay fees per calendar day.'),
('overdraft_transfer',      'Overdraft transfer from savings or loan', '$5.00 per transfer', ''),
('wire_in_domestic',        'Incoming domestic wire',              '$10.00', ''),
('wire_out_domestic_under_2500', 'Outgoing domestic wire under $2,500', '$15.00', ''),
('wire_out_domestic_2500_plus',  'Outgoing domestic wire of $2,500 or more', '$30.00', ''),
('wire_in_foreign',         'Incoming foreign wire',               '$40.00', ''),
('wire_out_foreign',        'Outgoing foreign wire',               '$50.00', ''),
('monthly_cashback',        'Cashback Rewards Checking monthly fee','$10.00 per month', 'Waived with $1,000 or more in monthly direct deposits, or a $5,000 average daily balance.'),
('monthly_star',            'STAR Checking monthly fee',           '$7.00 per month',  'Waived with a $500 minimum balance, or $10,000 in combined household balances.'),
('monthly_premiere',        'Premiere Checking monthly fee',       '$17.00 per month', 'Waived with a $5,000 average daily balance, or $25,000 in combined household balances.'),
('monthly_money_market',    'Money Market monthly fee',            '$10.00 per month', 'Waived with a $2,500 average daily balance, or within 60 days of opening. $2,500 minimum to open.'),
('hys_excess_withdrawal',   'High Yield Savings excess withdrawal','$25.00 per withdrawal', 'Applies beyond 3 free withdrawals per quarter.'),
('inactivity',              'Inactivity fee',                      '$5.00 per month',  'After 1 year of no activity; waived with $500 or more in combined deposits.'),
('paper_statement',         'Paper statement fee',                 '$2.00 per month',  'Waived for the first 60 days, for members under 21, and for members 70 or older.'),
('card_replacement',        'Card replacement',                    '$10.00',           'Free when the card was stolen.'),
('card_replacement_expedited_domestic',      'Expedited card delivery, domestic',      '$30.00', 'In addition to any replacement fee.'),
('card_replacement_expedited_international', 'Expedited card delivery, international', '$35.00', 'In addition to any replacement fee.'),
('atm_noncopperline_withdrawal', 'Non-Copperline ATM withdrawal',  '$3.00', ''),
('atm_noncopperline_inquiry',    'Non-Copperline ATM inquiry',     '$1.00', ''),
('cc_late_payment',         'Credit card late payment',            'up to $35.00', ''),
('cc_returned_payment',     'Credit card returned payment',        'up to $25.00', ''),
('cash_advance',            'Credit card cash advance',            '5.0% of the advance', '$10.00 minimum.'),
('balance_transfer',        'Credit card balance transfer',        '5.0% of the transfer', '$5.00 minimum.'),
('foreign_transaction',     'Credit card foreign transaction',     '1.1% of the transaction', 'Waived on the World card.'),
('stop_payment',            'Stop payment',                        '$25.00',           'No charge on Cashback Rewards Checking.'),
('cashiers_check',          'Cashier''s check',                    '$5.00', ''),
('deposited_item_copy',     'Copy of a deposited item',            '$10.00', ''),
('account_research',        'Account research or reconciliation',  '$50.00 per hour', ''),
('loan_payment_phone_echeck','Loan payment by phone, eCheck',      '$2.75', ''),
('loan_payment_phone_debit', 'Loan payment by phone, debit card',  '$5.50', ''),
('late_loan_payment',       'Late loan payment',                   '2% to 5% of the payment due', ''),
('heloc_early_termination', 'HELOC early termination',             '$250.00', ''),
('mortgage_modification',   'Mortgage modification',               '$1,000.00', ''),
('subordination',           'Home equity subordination',           '$100.00', ''),
('title_change',            'Vehicle title change',                '$50.00', ''),
('lien_release_letter',     'Lien release letter',                 '$10.00', '');

INSERT INTO membership_eligibility (kind, name, eligible, note) VALUES
('county', 'Bucks',        1, ''),
('county', 'Chester',      1, ''),
('county', 'Delaware',     1, ''),
('county', 'Lancaster',    1, ''),
('county', 'Montgomery',   1, ''),
('county', 'Philadelphia', 1, ''),
('county', 'Berks',        0, 'Not currently in the Copperline service area.'),
('employer', 'Marklin Steel retirees',        1, 'Founding common-bond group.'),
('employer', 'Granford Area School District', 1, ''),
('employer', 'Averton Regional Health',       1, '');

INSERT INTO kb (topic, keywords, answer) VALUES
('routing_number', 'routing,aba,routing number,direct deposit setup',
 'The Copperline routing number is 231380042.'),
('hours', 'hours,open,phone hours,member care,holiday',
 'Member care is available Monday through Friday 8am to 6pm, and Saturday 9am to 1pm, Eastern. This assistant answers 24/7. Branch hours vary by branch.'),
('legacy_names', 'marklin,marklin steel,granford credit union,copperline federal,old name,used to be called,merger',
 'Copperline Credit Union was founded in 1937 as Marklin Steel Employees Federal Credit Union, was later known as Copperline Federal Credit Union, and acquired Granford Credit Union in 2005. All of those are the same institution: accounts, cards, and loans carried over.'),
('id_theft', 'identity theft,id theft,identity protection,recovery,merchants',
 'Members enrolled in Copperline ID Theft Protection work with our recovery partner, Meridian Recovery Services, at 866-555-0119. Disputes on Copperline transactions are still handled by Copperline directly.'),
('membership', 'join,membership,become a member,eligibility,who can join',
 'Under the 2026 charter, membership is open to people who live or work in Bucks, Chester, Delaware, Lancaster, Montgomery, or Philadelphia counties in Pennsylvania, and to eligible employer groups.'),
('disputes_overview', 'dispute,unauthorized,fraud charge,billing error,chargeback',
 'A dispute can be filed on this call once identity is verified. Debit disputes follow federal error-resolution rules with provisional credit while we investigate; credit card billing errors follow federal billing-error rules.'),
('courtesy_pay_overview', 'courtesy pay,overdraft program,overdraft protection',
 'Courtesy Pay covers items when the account is short, at $33.00 per item, with at most 3 combined NSF and Courtesy Pay fees per day. A $5.00 overdraft transfer from savings is the cheaper alternative.');

-- ------------------------------------------------------------- members

INSERT INTO members (id, name, phone, dob, member_number_last4, member_since, exploitation_watch, courtesy_pay_fees_12mo) VALUES
('m_001', 'Marisol Vega',  '6105550142', '1988-03-14', '4471', 2011, 0, 0),
('m_002', 'Ray Delgado',   '4845550117', '1979-11-02', '9083', 2019, 0, 1),
('m_003', 'June Okafor',   '2155550163', '1990-06-21', '3327', 2016, 0, 2),
('m_004', 'Harold Brandt', '6105550178', '1945-02-09', '6640', 1987, 1, 0),
('m_005', 'Priya Raman',   '4845550190', '1994-09-30', '2214', 2021, 0, 0),
('m_006', 'Tom Keller',    '2675550151', '1985-01-17', '7752', 2018, 0, 0),
('m_007', 'Alma Reyes',    '6105550129', '1992-12-05', '5518', 2015, 0, 0),
('m_008', 'Walt Jessup',   '7175550136', '1958-04-26', '8804', 2002, 0, 0),
('m_009', 'Nina Sowell',   '4845550102', '1998-07-11', '1147', 2023, 0, 0);

INSERT INTO accounts (id, member_id, type, label, last4, balance_cents, available_cents, opened_date,
                      direct_deposit_cents, adb_cents, household_cents, withdrawals_this_quarter) VALUES
('a_001c', 'm_001', 'cashback_rewards',   'Cashback Rewards Checking', '3302',  241877,  238012, '2011-05-09', 145000, 260000, 0, 0),
('a_001s', 'm_001', 'high_yield_savings', 'High Yield Savings',        '8890', 1250000, 1250000, '2014-02-20', 0, 0, 0, 1),
('a_002c', 'm_002', 'free_checking',      'FREE Checking',             '7714',   31240,   26815, '2019-08-01', 0, 0, 0, 0),
('a_003c', 'm_003', 'star_checking',      'STAR Checking',             '2209',   87650,   84210, '2016-03-11', 0, 62000, 0, 0),
('a_004c', 'm_004', 'premiere_checking',  'Premiere Checking',         '9911', 4820000, 4820000, '1987-06-15', 0, 4700000, 5100000, 0),
('a_005s', 'm_005', 'high_yield_savings', 'High Yield Savings',        '4407',  980000,  980000, '2021-10-04', 0, 0, 0, 3),
('a_005c', 'm_005', 'free_checking',      'FREE Checking',             '1180',  142055,  142055, '2021-10-04', 0, 0, 0, 0),
('a_006c', 'm_006', 'cashback_rewards',   'Cashback Rewards Checking', '6633',  418099,  418099, '2018-01-25', 80000, 420000, 0, 0),
('a_007c', 'm_007', 'free_checking',      'FREE Checking',             '8802',  195322,  195322, '2015-04-30', 0, 0, 0, 0),
('a_007k', 'm_007', 'credit_card',        'Copperline Mastercard',     '4419', -134255, -134255, '2017-09-12', 0, 0, 0, 0),
('a_008c', 'm_008', 'free_checking',      'FREE Checking',             '3319',  268840,  268840, '2002-11-19', 0, 0, 0, 0),
('a_009c', 'm_009', 'free_checking',      'FREE Checking',             '2288',  190000,  190000, '2023-06-02', 0, 0, 0, 0);

-- Transactions. Fee rows carry fee_code; appsn=1 marks authorized-positive/settled-negative.
-- statement_date drives the 60-day dispute-window math against TODAY = 2026-08-01.
INSERT INTO transactions (id, account_id, posted, description, amount_cents, kind, fee_code, appsn, statement_date) VALUES
-- Marisol: ordinary activity
('t_101', 'a_001c', '2026-07-30', 'Payroll deposit — Averton Regional Health', 145000, 'deposit',  '', 0, ''),
('t_102', 'a_001c', '2026-07-29', 'Granford Grocers',                          -8742, 'purchase', '', 0, ''),
('t_103', 'a_001c', '2026-07-27', 'Keystone Utilities autopay',               -12680, 'payment',  '', 0, ''),
-- Ray: the APPSN trap — purchase authorized on a positive balance, settled negative, fee followed
('t_201', 'a_002c', '2026-07-26', 'Hendy''s Market',                           -4187, 'purchase', '', 1, '2026-07-28'),
('t_202', 'a_002c', '2026-07-28', 'Courtesy Pay fee',                          -3300, 'fee', 'courtesy_pay', 1, '2026-07-28'),
('t_203', 'a_002c', '2026-07-25', 'Fuel stop — Pell Creek',                    -3020, 'purchase', '', 0, ''),
-- June: second Courtesy Pay fee inside 12 months
('t_301', 'a_003c', '2026-07-25', 'Courtesy Pay fee',                          -3300, 'fee', 'courtesy_pay', 0, '2026-07-28'),
('t_302', 'a_003c', '2026-07-24', 'Danbrook Auto Service',                    -21500, 'purchase', '', 0, ''),
-- Tom: monthly maintenance fee, waiver missed on both conditions
('t_601', 'a_006c', '2026-07-31', 'Monthly maintenance fee',                   -1000, 'fee', 'monthly_cashback', 0, '2026-07-31'),
('t_602', 'a_006c', '2026-07-15', 'Payroll deposit — Keller Cabinetry',        80000, 'deposit',  '', 0, ''),
-- Alma: in-window unauthorized debit + a credit-card billing error (duplicate)
('t_701', 'a_007c', '2026-07-02', 'RIDGELINE ELECTRONICS',                    -21456, 'purchase', '', 0, '2026-07-05'),
('t_702', 'a_007c', '2026-07-01', 'Harrow Mills Pharmacy',                     -2318, 'purchase', '', 0, '2026-07-05'),
('t_711', 'a_007k', '2026-06-30', 'STREAMCO monthly',                          -8900, 'purchase', '', 0, '2026-07-05'),
('t_712', 'a_007k', '2026-06-30', 'STREAMCO monthly',                          -8900, 'purchase', '', 0, '2026-07-05'),
-- Walt: unauthorized debit, statement 71 days ago — outside the 60-day window
('t_801', 'a_008c', '2026-05-19', 'QUICKPARTS LLC',                           -13000, 'purchase', '', 0, '2026-05-22'),
('t_802', 'a_008c', '2026-07-20', 'Pell Creek Hardware',                       -4611, 'purchase', '', 0, '2026-07-22'),
-- Nina: ordinary checking activity
('t_901', 'a_009c', '2026-07-28', 'Payroll deposit — Danbrook Public Library', 96000, 'deposit',  '', 0, '');

INSERT INTO cards (id, member_id, type, last4, status, block_reason) VALUES
('card_001', 'm_001', 'debit',  '5512', 'active', ''),
('card_002', 'm_002', 'debit',  '7741', 'active', ''),
('card_003', 'm_003', 'debit',  '3358', 'active', ''),
('card_004', 'm_004', 'debit',  '6001', 'active', ''),
('card_005', 'm_005', 'debit',  '9924', 'active', ''),
('card_007d', 'm_007', 'debit',  '2246', 'active', ''),
('card_007k', 'm_007', 'credit', '4419', 'active', ''),
('card_008', 'm_008', 'debit',  '7180', 'active', ''),
('card_009', 'm_009', 'debit',  '5077', 'active', '');

INSERT INTO loans (id, member_id, type, label, last4, balance_cents, payment_due_cents, due_date) VALUES
('l_009', 'm_009', 'auto',  'Auto loan',            '5561', 1874200, 38942, '2026-08-10'),
('l_004', 'm_004', 'heloc', 'Home equity line',     '3090', 4200000, 21500, '2026-08-15');
