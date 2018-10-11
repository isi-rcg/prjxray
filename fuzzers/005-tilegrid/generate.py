#!/usr/bin/env python3
'''
Historically we grouped data into "segments"
These were a region of the bitstream that encoded one or more tiles
However, this didn't scale with certain tiles like BRAM
Some sites had multiple bitstream areas and also occupied multiple tiles

Decoding was then shifted to instead describe how each title is encoded
A post processing step verifies that two tiles don't reference the same bitstream area
'''

import os, sys, json, re

# matches lib/include/prjxray/xilinx/xc7series/block_type.h
block_type_i2s = {
    0: 'CLB_IO_CLK',
    1: 'BLOCK_RAM',
    2: 'CFG_CLB',
    # special...maybe should error until we know what it is?
    # 3: 'RESERVED',
}


def addr2btype(base_addr):
    '''
    Convert integer address to block type

    Table 5-24: Frame Address Register Description
    Bit Index: [25:23]
    https://www.xilinx.com/support/documentation/user_guides/ug470_7Series_Config.pdf
    "Valid block types are CLB, I/O, CLK ( 000 ), block RAM content ( 001 ), and CFG_CLB ( 010 ). A normal bitstream does not include type 011 ."
    '''
    block_type_i = (base_addr >> 23) & 0x7
    return block_type_i2s[block_type_i]


def nolr(tile_type):
    '''
    Remove _L or _R suffix tile_type suffix, if present
    Ex: BRAM_INT_INTERFACE_L => BRAM_INT_INTERFACE
    Ex: VBRK => VBRK
    '''
    postfix = tile_type[-2:]
    if postfix in ('_L', '_R'):
        return tile_type[:-2]
    else:
        return tile_type


def load_tiles(tiles_fn):
    '''
    "$type $tile $grid_x $grid_y $typed_sites"
    typed_sites: foreach t $site_types s $sites
    '''
    tiles = list()

    with open(tiles_fn) as f:
        for line in f:
            # CLBLM_L CLBLM_L_X10Y98 30 106 SLICEL SLICE_X13Y98 SLICEM SLICE_X12Y98
            record = line.split()
            tile_type, tile_name, grid_x, grid_y = record[0:4]
            grid_x, grid_y = int(grid_x), int(grid_y)
            sites = {}
            for i in range(4, len(record), 2):
                site_type, site_name = record[i:i + 2]
                sites[site_name] = site_type
            tile = {
                'type': tile_type,
                'name': tile_name,
                'grid_x': grid_x,
                'grid_y': grid_y,
                'sites': sites,
            }
            tiles.append(tile)

    return tiles


def load_baseaddrs(deltas_fns):
    site_baseaddr = dict()
    for arg in deltas_fns:
        with open(arg) as f:
            line = f.read().strip()
            site = arg[7:-6]
            frame = int(line[5:5 + 8], 16)
            # was "0x%08x"
            site_baseaddr[site] = frame & ~0x7f

    return site_baseaddr


def make_database(tiles):
    # tile database with X, Y, and list of sites
    # tile name as keys
    database = dict()

    for tile in tiles:
        database[tile["name"]] = {
            "type": tile["type"],
            "sites": tile["sites"],
            "grid_x": tile["grid_x"],
            "grid_y": tile["grid_y"],
        }

    return database


def make_tile_baseaddrs(tiles, site_baseaddr, verbose=False):
    # Look up a base address by tile name
    tile_baseaddrs = dict()

    verbose and print('')
    for tile in tiles:
        for site_name in tile["sites"].keys():
            if site_name not in site_baseaddr:
                continue
            framebaseaddr = site_baseaddr[site_name]
            bt = addr2btype(framebaseaddr)
            tile_baseaddr = tile_baseaddrs.setdefault(tile["name"], {})
            if bt in tile_baseaddr:
                # actually lets just fail these, better to remove at tcl level to speed up processing
                assert 0, 'duplicate base address'
                assert tile_baseaddr[bt] == [framebaseaddr, 0]
            else:
                tile_baseaddr[bt] = [framebaseaddr, 0]
            verbose and print(
                "baseaddr: %s.%s @ %s.0x%08x" %
                (tile["name"], site_name, bt, framebaseaddr))

    return tile_baseaddrs


def make_tiles_by_grid(tiles):
    # lookup tile names by (X, Y)
    tiles_by_grid = dict()

    for tile in tiles:
        tiles_by_grid[(tile["grid_x"], tile["grid_y"])] = tile["name"]

    return tiles_by_grid


def make_segments(database, tiles_by_grid, tile_baseaddrs, verbose=False):
    '''
    Create segments data structure
    Indicates how tiles are related to bitstream locations
    Also modify database to annotate which segment the tiles belong to

    segments key examples:
        SEG_CLBLM_R_X13Y72
        SEG_BRAM3_L_X6Y85
    '''
    segments = dict()

    verbose and print('')
    for tile_name, tile_data in database.items():
        tile_type = tile_data["type"]
        grid_x = tile_data["grid_x"]
        grid_y = tile_data["grid_y"]

        def add_segment(name, tiles, segtype, frames, words, baseaddr=None):
            assert name not in segments
            segment = segments.setdefault(name, {})
            segment["tiles"] = tiles
            segment["type"] = segtype
            segment["frames"] = frames
            segment["words"] = words
            if baseaddr:
                verbose and print(
                    'make_segment: %s baseaddr %s' % (
                        name,
                        baseaddr,
                    ))
                segment["baseaddr"] = baseaddr

            for tile_name in tiles:
                database[tile_name]["segment"] = name

        def process_clb():
            if tile_type in ["CLBLL_L", "CLBLM_L"]:
                int_tile_name = tiles_by_grid[(grid_x + 1, grid_y)]
            else:
                int_tile_name = tiles_by_grid[(grid_x - 1, grid_y)]

            add_segment(
                name="SEG_" + tile_name,
                tiles=[tile_name, int_tile_name],
                segtype=tile_type.lower(),
                frames=36,
                words=2,
                baseaddr=tile_baseaddrs.get(tile_name, None))

        def process_hclk():
            add_segment(
                name="SEG_" + tile_name,
                tiles=[tile_name],
                segtype=tile_type.lower(),
                frames=26,
                words=1)

        def process_bram_dsp():
            for k in range(5):
                if tile_type in ["BRAM_L", "DSP_L"]:
                    interface_tile_name = tiles_by_grid[(
                        grid_x + 1, grid_y - k)]
                    int_tile_name = tiles_by_grid[(grid_x + 2, grid_y - k)]
                elif tile_type in ["BRAM_R", "DSP_R"]:
                    interface_tile_name = tiles_by_grid[(
                        grid_x - 1, grid_y - k)]
                    int_tile_name = tiles_by_grid[(grid_x - 2, grid_y - k)]
                else:
                    assert 0
                '''
                BRAM/DSP itself is at the base y address
                There is one huge switchbox on the right for the 5 tiles
                These fan into 5 BRAM_INT_INTERFACE tiles each which feed into their own CENTER_INTER (just like a CLB has) 
                '''
                if k == 0:
                    tiles = [tile_name, interface_tile_name, int_tile_name]
                    baseaddr = tile_baseaddrs.get(tile_name, None)
                else:
                    tiles = [interface_tile_name, int_tile_name]
                    baseaddr = None

                add_segment(
                    # BRAM_L_X6Y70 => SEG_BRAM4_L_X6Y70
                    name="SEG_" + tile_name.replace("_", "%d_" % k, 1),
                    tiles=tiles,
                    # BRAM_L => bram4_l
                    segtype=tile_type.lower().replace("_", "%d_" % k, 1),
                    frames=28,
                    words=2,
                    baseaddr=baseaddr)

        def process_default():
            #verbose and nolr(tile_type) not in ('VBRK', 'INT', 'NULL') and print('make_segment: drop %s' % (tile_type,))
            pass

        {
            "CLBLL": process_clb,
            "CLBLM": process_clb,
            "HCLK": process_hclk,
            "BRAM": process_bram_dsp,
            "DSP": process_bram_dsp,
        }.get(nolr(tile_type), process_default)()

    return segments


def get_inttile(database, segment):
    '''Return interconnect tile for given segment'''
    inttiles = [
        tile for tile in segment["tiles"]
        if database[tile]["type"] in ["INT_L", "INT_R"]
    ]
    assert len(inttiles) == 1
    return inttiles[0]


def seg_base_addr_lr_INT(database, segments, tiles_by_grid, verbose=False):
    '''Populate segment base addresses: L/R along INT column'''
    '''
    Create BRAM base addresses based on nearby CLBs
    ie if we have a BRAM_L, compute as nearby CLB_R base address + offset 
    '''

    verbose and print('')
    for segment_name in sorted(segments.keys()):
        segment = segments[segment_name]
        baseaddrs = segment.get("baseaddr", None)
        if not baseaddrs:
            continue

        for block_type, (framebase, wordbase) in sorted(baseaddrs.items()):
            verbose and print(
                'lr_INT: %s: %s.0x%08X:%u' %
                (segment_name, block_type, framebase, wordbase))
            if block_type != 'CLB_IO_CLK':
                verbose and print('  Skip non CLB')
                continue

            inttile = get_inttile(database, segment)
            grid_x = database[inttile]["grid_x"]
            grid_y = database[inttile]["grid_y"]

            if database[inttile]["type"] == "INT_L":
                grid_x += 1
                framebase = framebase + 0x80
            elif database[inttile]["type"] == "INT_R":
                grid_x -= 1
                framebase = framebase - 0x80
            else:
                assert 0

            # ROI at edge?
            if (grid_x, grid_y) not in tiles_by_grid:
                verbose and print('  Skip edge')
                continue

            tile = tiles_by_grid[(grid_x, grid_y)]

            if database[inttile]["type"] == "INT_L":
                assert database[tile]["type"] == "INT_R"
            elif database[inttile]["type"] == "INT_R":
                assert database[tile]["type"] == "INT_L"
            else:
                assert 0

            assert "segment" in database[tile]

            seg = database[tile]["segment"]

            seg_baseaddrs = segments[seg].setdefault("baseaddr", {})
            # At least one duplicate when we re-compute the entry for the base address
            # should give the same address
            if block_type in seg_baseaddrs:
                assert seg_baseaddrs[block_type] == [
                    framebase, wordbase
                ], (seg_baseaddrs[block_type], [framebase, wordbase])
                verbose and print('  Existing OK')
            else:
                seg_baseaddrs[block_type] = [framebase, wordbase]
                verbose and print('  Add new')


def seg_base_addr_up_INT(database, segments, tiles_by_grid, verbose=False):
    '''Populate segment base addresses: Up along INT/HCLK columns'''

    verbose and print('')
    '''
    All baseaddrs so far have 50 tiles above them to be derived
    However, once we start deriving, this is no longer true
    Copy the initial list so that any baseaddr encountered can safely be swept up
    '''
    src_segment_names = list()
    for segment_name in segments.keys():
        if "baseaddr" in segments[segment_name]:
            src_segment_names.append(segment_name)

    verbose and print('up_INT: %u base addresses' % len(src_segment_names))
    #verbose and print('\n'.join(sorted(src_segment_names)))

    for src_segment_name in sorted(src_segment_names):
        src_segment = segments[src_segment_name]

        for block_type, (framebase,
                         wordbase) in sorted(src_segment["baseaddr"].items()):
            verbose and print(
                'up_INT: %s: %s.0x%08X:%u' %
                (src_segment_name, block_type, framebase, wordbase))
            # Ignore BRAM base addresses
            # TODO: BRAM data needs to be populated in its own special way
            if block_type != 'CLB_IO_CLK':
                verbose and print('  Skip non CLB')
                continue

            inttile = get_inttile(database, src_segment)
            verbose and print(
                '  up_INT: %s => inttile %s' % (src_segment_name, inttile))
            grid_x = database[inttile]["grid_x"]
            grid_y = database[inttile]["grid_y"]

            for i in range(50):
                grid_y -= 1
                dst_tile = database[tiles_by_grid[(grid_x, grid_y)]]

                if wordbase == 50:
                    wordbase += 1
                else:
                    wordbase += 2

                #verbose and print('  dst_tile', dst_tile)
                dst_segment_name = dst_tile["segment"]
                #verbose and print('up_INT: %s => %s' % (src_segment_name, dst_segment_name))
                segments[dst_segment_name].setdefault(
                    "baseaddr", {})[block_type] = [framebase, wordbase]


def add_tile_bits(tile_db, baseaddr, offset, height):
    '''
    Record data structure geometry for the given tile baseaddr
    For most tiles there is only one baseaddr, but some like BRAM have multiple

    Notes on multiple block types:
    https://github.com/SymbiFlow/prjxray/issues/145
    '''
    bits = tile_db.setdefault('bits', {})
    block_type = addr2btype(baseaddr)

    assert block_type not in bits
    block = bits.setdefault(block_type, {})

    block["baseaddr"] = '0x%08X' % baseaddr
    block["offset"] = offset
    block["height"] = height


def add_bits(database, segments):
    '''Transfer segment data into tiles'''
    for segment_name in segments.keys():
        for _block_type, (
                baseaddr,
                offset) in segments[segment_name]["baseaddr"].items():
            for tile_name in segments[segment_name]["tiles"]:
                tile_type = database[tile_name]["type"]
                height = {
                    "CLBLL": 2,
                    "CLBLM": 2,
                    "INT": 2,
                    "HCLK": 1,
                    "BRAM": 10,
                    "DSP": 10,
                    "INT_INTERFACE": 0,
                    "BRAM_INT_INTERFACE": 0,
                }.get(nolr(tile_type), None)
                if height is None:
                    raise ValueError("Unknown tile type %s" % tile_type)
                if height:
                    add_tile_bits(
                        database[tile_name], baseaddr, offset, height)


def annotate_segments(database, segments):
    # TODO: Migrate to new tilegrid format via library.  This data is added for
    # compability with unconverted tools.  Update tools then remove this data from
    # tilegrid.json.
    for tiledata in database.values():
        if "segment" in tiledata:
            segment = tiledata["segment"]
            tiledata["frames"] = segments[segment]["frames"]
            tiledata["words"] = segments[segment]["words"]
            tiledata["segment_type"] = segments[segment]["type"]


def run(tiles_fn, json_fn, deltas_fns, verbose=False):
    # Load input files
    tiles = load_tiles(tiles_fn)
    site_baseaddr = load_baseaddrs(deltas_fns)

    # Index input
    database = make_database(tiles)
    tile_baseaddrs = make_tile_baseaddrs(tiles, site_baseaddr, verbose=verbose)
    tiles_by_grid = make_tiles_by_grid(tiles)

    segments = make_segments(
        database, tiles_by_grid, tile_baseaddrs, verbose=verbose)

    # Reference adjacent CLBs to locate adjacent tiles by known offsets
    seg_base_addr_lr_INT(database, segments, tiles_by_grid, verbose=verbose)
    seg_base_addr_up_INT(database, segments, tiles_by_grid, verbose=verbose)

    add_bits(database, segments)
    annotate_segments(database, segments)

    # Save
    json.dump(
        database,
        open(json_fn, 'w'),
        sort_keys=True,
        indent=4,
        separators=(',', ': '))


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description=
        'Generate a simple wrapper to test synthesizing an arbitrary verilog module'
    )

    parser.add_argument('--verbose', action='store_true', help='')
    parser.add_argument('--out', default='/dev/stdout', help='Output JSON')
    parser.add_argument(
        '--tiles', default='tiles.txt', help='Input tiles.txt tcl output')
    parser.add_argument(
        'deltas', nargs='+', help='.bit diffs to create base addresses from')
    args = parser.parse_args()

    run(args.tiles, args.out, args.deltas, verbose=args.verbose)


if __name__ == '__main__':
    main()
