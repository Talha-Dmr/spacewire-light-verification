-- Formal harness for spwlink (SpaceWire exchange level, ECSS-E-ST-50-12C 8.5)
-- Checked with SymbiYosys + GHDL (PSL assertions, VHDL-2008).
--
-- The environment (receiver, transmitter, application) is free except for
-- the interface contracts documented in spwpkg.vhd. Credit counters of the
-- DUT are shadowed from the interface events; every property below is
-- therefore stated purely in terms of the DUT's ports.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.spwpkg.all;

entity spwlink_formal is
    port (
        clk:        in  std_logic;
        rst:        in  std_logic;
        -- application side (free)
        autostart:  in  std_logic;
        linkstart:  in  std_logic;
        linkdis:    in  std_logic;
        rxroom:     in  std_logic_vector(5 downto 0);
        tick_in:    in  std_logic;
        txwrite:    in  std_logic;
        -- receiver side (free, constrained by the spwrecv contract)
        gotbit:     in  std_logic;
        gotnull:    in  std_logic;
        gotfct:     in  std_logic;
        tick_rx:    in  std_logic;
        rxchar:     in  std_logic;
        errdisc:    in  std_logic;
        errpar:     in  std_logic;
        erresc:     in  std_logic;
        -- transmitter side (free, constrained by the spwxmit contract)
        fctack:     in  std_logic;
        txack:      in  std_logic
    );
end entity;

architecture formal of spwlink_formal is
    constant RESET_TIME: integer := 3;   -- 6.4 us timer shortened for tractable depth

    signal linki:   spw_link_in_type;
    signal linko:   spw_link_out_type;
    signal rxen:    std_logic;
    signal recvo:   spw_recv_out_type;
    signal xmiti:   spw_xmit_in_type;
    signal xmito:   spw_xmit_out_type;

    signal init:        std_logic := '1';
    -- shadow credit counters (same 6-bit wrap-around arithmetic as the DUT)
    signal s_rx, s_tx:  unsigned(5 downto 0) := (others => '0');
    signal s_rx_q, s_tx_q: unsigned(5 downto 0) := (others => '0');
    -- one/two-cycle history
    signal rxroom_q, rxroom_q2: unsigned(5 downto 0) := (others => '0');
    signal rxchar_q, gotfct_q, gotnull_q, rxen_q, linkdis_q: std_logic := '0';
    signal started_q, connecting_q, running_q, errcred_q: std_logic := '0';
    -- dwell counters
    signal reset_cnt, started_cnt, connecting_cnt: unsigned(4 downto 0) := (others => '0');
begin
    linki.autostart <= autostart;
    linki.linkstart <= linkstart;
    linki.linkdis   <= linkdis;
    linki.rxroom    <= rxroom;
    linki.tick_in   <= tick_in;
    linki.ctrl_in   <= "00";
    linki.time_in   <= (others => '0');
    linki.txwrite   <= txwrite;
    linki.txflag    <= '0';
    linki.txdata    <= (others => '0');

    recvo.gotbit    <= gotbit;
    recvo.gotnull   <= gotnull;
    recvo.gotfct    <= gotfct;
    recvo.tick_out  <= tick_rx;
    recvo.ctrl_out  <= "00";
    recvo.time_out  <= (others => '0');
    recvo.rxchar    <= rxchar;
    recvo.rxflag    <= '0';
    recvo.rxdata    <= (others => '0');
    recvo.errdisc   <= errdisc;
    recvo.errpar    <= errpar;
    recvo.erresc    <= erresc;

    xmito.fctack    <= fctack;
    xmito.txack     <= txack;

    dut: spwlink
        generic map ( reset_time => RESET_TIME )
        port map ( clk => clk, rst => rst, linki => linki, linko => linko,
                   rxen => rxen, recvo => recvo, xmiti => xmiti, xmito => xmito );

    -- shadow model and history
    process (clk) is
    begin
        if rising_edge(clk) then
            init        <= '0';
            rxroom_q    <= unsigned(rxroom);
            rxroom_q2   <= rxroom_q;
            rxchar_q    <= rxchar;
            gotfct_q    <= gotfct;
            gotnull_q   <= gotnull;
            rxen_q      <= rxen;
            linkdis_q   <= linkdis;
            started_q   <= linko.started;
            connecting_q <= linko.connecting;
            running_q   <= linko.running;
            errcred_q   <= linko.errcred;
            s_rx_q      <= s_rx;
            s_tx_q      <= s_tx;
            if rst = '1' or rxen = '0' then      -- DUT resets credit in ErrorReset
                s_rx <= (others => '0');
                s_tx <= (others => '0');
            else
                if gotfct = '1' and txack = '1' then
                    s_tx <= s_tx + 7;
                elsif gotfct = '1' then
                    s_tx <= s_tx + 8;
                elsif txack = '1' then
                    s_tx <= s_tx - 1;
                end if;
                if fctack = '1' and rxchar = '1' then
                    s_rx <= s_rx + 7;
                elsif fctack = '1' then
                    s_rx <= s_rx + 8;
                elsif rxchar = '1' then
                    s_rx <= s_rx - 1;
                end if;
            end if;
            if rst = '1' or rxen = '1' then reset_cnt <= (others => '0'); else reset_cnt <= reset_cnt + 1; end if;
            if linko.started = '1' then started_cnt <= started_cnt + 1; else started_cnt <= (others => '0'); end if;
            if linko.connecting = '1' then connecting_cnt <= connecting_cnt + 1; else connecting_cnt <= (others => '0'); end if;
        end if;
    end process;

    -- psl default clock is rising_edge(clk);

    -- ---------------- environment assumptions
    -- single synchronous reset at time zero
    -- psl a_01: assume always (init = '1') -> (rst = '1');
    -- psl a_02: assume always (init = '0') -> (rst = '0');
    -- spwrecv contract: nothing decoded before the first NULL; gotnull sticky while enabled;
    -- outputs quiet the cycle after the receiver was disabled; one token per cycle
    -- psl a_03: assume always (gotnull = '0') -> (gotfct = '0' and tick_rx = '0' and rxchar = '0' and errpar = '0' and erresc = '0');
    -- psl a_04: assume always (gotnull_q = '1' and rxen_q = '1') -> (gotnull = '1');
    -- psl a_05: assume always (rxen_q = '0') -> (gotnull = '0' and gotfct = '0' and tick_rx = '0' and rxchar = '0' and errpar = '0' and erresc = '0' and errdisc = '0');
    -- psl a_06: assume always not (gotfct = '1' and tick_rx = '1');
    -- psl a_07: assume always not (gotfct = '1' and rxchar = '1');
    -- psl a_08: assume always not (tick_rx = '1' and rxchar = '1');
    -- spwxmit contract: acks only for requests, never both in one cycle
    -- psl a_09: assume always (fctack = '1') -> (xmiti.fct_in = '1');
    -- psl a_10: assume always (txack = '1') -> (xmiti.txwrite = '1');
    -- psl a_11: assume always not (fctack = '1' and txack = '1');
    -- spwxmit acks only while enabled: FCT after its first NULL (Connecting/Run), N-chars in Run
    -- psl a_11b: assume always (fctack = '1') -> (linko.connecting = '1' or linko.running = '1');
    -- psl a_11c: assume always (txack = '1') -> (linko.running = '1');
    -- rxroom contract (spwpkg): may drop by one only in the cycle after rxchar
    -- psl a_12: assume always (init = '0' and rxchar_q = '0') -> (unsigned(rxroom) >= rxroom_q);
    -- psl a_13: assume always (init = '0' and rxchar_q = '1') -> (resize(unsigned(rxroom), 7) + 1 >= resize(rxroom_q, 7));

    -- ---------------- state machine structure (ECSS 8.5.2)
    -- psl a_14: assert always (linko.started = '1') -> (linko.connecting = '0' and linko.running = '0');
    -- psl a_15: assert always (linko.connecting = '1') -> (linko.running = '0');
    -- psl a_16: assert always (rxen = '0') -> (linko.started = '0' and linko.connecting = '0' and linko.running = '0');
    -- psl a_17: assert always (xmiti.txen = '1') -> (linko.started = '1' or linko.connecting = '1' or linko.running = '1');
    -- psl a_18: assert always (linko.started = '1' or linko.connecting = '1' or linko.running = '1') -> (xmiti.txen = '1');
    -- psl a_19: assert always (xmiti.stnull = '1') -> (linko.started = '1');
    -- psl a_20: assert always (linko.started = '1') -> (xmiti.stnull = '1');
    -- psl a_21: assert always (xmiti.stfct = '1') -> (linko.connecting = '1');
    -- psl a_22: assert always (linko.connecting = '1') -> (xmiti.stfct = '1');
    -- Run is entered from Connecting only, Connecting from Started only
    -- psl a_23: assert always (init = '0' and linko.running = '1') -> (running_q = '1' or connecting_q = '1');
    -- psl a_24: assert always (init = '0' and linko.connecting = '1') -> (connecting_q = '1' or started_q = '1');
    -- Ready -> Started needs room for one FCT and no link disable
    -- psl a_25: assert always (init = '0' and linko.started = '1' and started_q = '0') -> (rxroom_q2 >= 8 and linkdis_q = '0');
    -- errors reported only in Run and every error/disable leaves Run at once
    -- psl a_26: assert always (linko.errdisc = '1' or linko.errpar = '1' or linko.erresc = '1') -> (linko.running = '1');
    -- psl a_27: assert always (linko.running = '1' and (errdisc = '1' or errpar = '1' or erresc = '1' or linkdis = '1')) -> next (rxen = '0');
    -- psl a_28: assert always (linko.errcred = '1') -> (rxen = '0' or next (rxen = '0'));
    -- ErrorReset lasts exactly RESET_TIME+1 cycles; Started/Connecting time out after 2*(RESET_TIME+1)
    -- psl a_29: assert always (rxen = '0') -> (reset_cnt <= RESET_TIME);
    -- psl a_30: assert always (rxen = '0' and reset_cnt = RESET_TIME) -> next (rxen = '1');
    -- psl a_31: assert always (rxen = '0' and reset_cnt < RESET_TIME) -> next (rxen = '0');
    -- psl a_32: assert always (linko.started = '1') -> (started_cnt <= 2 * RESET_TIME + 1);
    -- psl a_33: assert always (linko.connecting = '1') -> (connecting_cnt <= 2 * RESET_TIME + 1);
    -- N-chars and time-codes are passed to the application in Run only
    -- psl a_34: assert always (linko.tick_out = '1' or linko.rxchar = '1') -> (linko.running = '1');
    -- psl a_35: assert always (xmiti.tick_in = '1') -> (linko.running = '1');

    -- ---------------- flow control (ECSS 8.5.3)
    -- credit granted to the remote never exceeds 56 and never exceeds RX room
    -- psl a_36: assert always (s_rx <= 56 or linko.errcred = '1');   -- wraps to 63 on a credit violation, then errcred
    -- psl a_37: assert always (rxen = '1' and linko.errcred = '0') -> (s_rx <= unsigned(rxroom));
    -- an FCT is granted only if it keeps credit <= 56 and within RX room (outside the credit-error cycle)
    -- psl a_38: assert always (fctack = '1' and linko.errcred = '0') -> (s_rx <= 48 and resize(s_rx, 7) + 8 <= resize(unsigned(rxroom), 7));
    -- credit from the remote never exceeds 56 unless a credit error is being raised
    -- psl a_39: assert always (s_tx <= 56 or linko.errcred = '1');
    -- N-chars are handed to the transmitter only with credit
    -- psl a_40: assert always (xmiti.txwrite = '1') -> (txwrite = '1' and s_tx /= 0);
    -- credit error exactly when the remote overruns credit or over-grants
    -- psl a_41: assert always (rxen = '1' and ((gotfct = '1' and s_tx > 48) or (rxchar = '1' and s_rx = 0))) -> next (linko.errcred = '1');
    -- psl a_42: assert always (init = '0' and linko.errcred = '1' and errcred_q = '0') -> (rxen_q = '1' and ((gotfct_q = '1' and s_tx_q > 48) or (rxchar_q = '1' and s_rx_q = 0)));

    -- ---------------- reachability
    -- psl c_43: cover {linko.running = '1'};
    -- psl c_44: cover {linko.running = '1' and fctack = '1'};
    -- psl c_45: cover {linko.running = '1' and txack = '1'};
    -- psl c_46: cover {linko.errcred = '1'};
    -- psl c_47: cover {linko.running = '1' and linko.errdisc = '1'};
    -- psl c_48: cover {linko.started = '1' and started_cnt = 2 * RESET_TIME + 1};
end architecture;
