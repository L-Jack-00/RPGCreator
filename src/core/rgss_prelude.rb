# -*- coding: utf-8 -*-
# Clean-Room RGSS1 (RMXP) + RGSS3 (VX Ace) Ruby Runtime Prelude for mruby VM.
# Copyright-safe: defines only public API classes, primitives, and host bridges;
# user-provided official scripts (Game_*, Scene_*, Window_*) and RTP load on top.

unless Object.const_defined?(:RGSSReset)
  class RGSSReset < Exception; end
end
unless Object.const_defined?(:RGSSError)
  class RGSSError < StandardError; end
end

unless Object.const_defined?(:Encoding)
  module Encoding
    UTF_8 = "UTF-8"
    ASCII_8BIT = "ASCII-8BIT"
    BINARY = "ASCII-8BIT"
    def self.find(name); name.to_s; end
  end
end

class String
  def force_encoding(_enc); self; end unless method_defined?(:force_encoding)
  def encode(_dst, _src = nil, **_opts); self; end unless method_defined?(:encode)
  def ord
    return 0 if empty?
    bytes[0]
  end unless method_defined?(:ord)
  def getbyte(i)
    bytes[i]
  end unless method_defined?(:getbyte)
end

if Object.const_defined?(:IO)
  class IO
    def self.binread(path, length = nil, offset = 0)
      File.open(path.to_s, "rb") do |f|
        f.seek(offset.to_i) if offset && offset.to_i > 0
        length ? f.read(length.to_i) : f.read
      end
    end unless respond_to?(:binread)
    def self.read(path, length = nil, offset = 0)
      binread(path, length, offset)
    end unless respond_to?(:read)
  end
end

module RPGCBase64
  ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"

  def self.encode(str)
    s = str.to_s
    out = ""
    i = 0
    len = s.bytesize
    bs = s.bytes
    while i < len
      b0 = bs[i]
      b1 = i + 1 < len ? bs[i + 1] : 0
      b2 = i + 2 < len ? bs[i + 2] : 0
      out << ALPHABET[(b0 >> 2) & 0x3F]
      out << ALPHABET[((b0 & 0x03) << 4) | ((b1 >> 4) & 0x0F)]
      out << (i + 1 < len ? ALPHABET[((b1 & 0x0F) << 2) | ((b2 >> 6) & 0x03)] : "=")
      out << (i + 2 < len ? ALPHABET[b2 & 0x3F] : "=")
      i += 3
    end
    out
  end

  def self.decode(str)
    rev = {}
    ALPHABET.each_char.with_index { |c, idx| rev[c] = idx }
    clean = +""
    str.to_s.each_char { |c| clean << c if rev.key?(c) || c == "=" }
    out = []
    i = 0
    while i + 3 < clean.length
      c0 = rev[clean[i]] || 0
      c1 = rev[clean[i + 1]] || 0
      c2 = clean[i + 2] == "=" ? nil : rev[clean[i + 2]]
      c3 = clean[i + 3] == "=" ? nil : rev[clean[i + 3]]
      out << (((c0 << 2) | (c1 >> 4)) & 0xFF)
      out << ((((c1 & 0x0F) << 4) | (c2 >> 2)) & 0xFF) if c2
      out << ((((c2 & 0x03) << 6) | c3) & 0xFF) if c2 && c3
      i += 4
    end
    out.pack("C*")
  end
end

module RPGCJSON
  def self.dump(obj)
    case obj
    when nil
      "null"
    when true
      "true"
    when false
      "false"
    when Integer, Float
      obj.to_s
    when Symbol
      dump_str(obj.to_s)
    when String
      dump_str(obj)
    when Array
      "[" + obj.map { |v| dump(v) }.join(",") + "]"
    when Hash
      pairs = obj.map { |k, v| dump_str(k.to_s) + ":" + dump(v) }
      "{" + pairs.join(",") + "}"
    else
      if obj.respond_to?(:to_h)
        dump(obj.to_h)
      else
        dump_str(obj.to_s)
      end
    end
  end

  def self.dump_str(s)
    out = '"'
    s.each_char do |ch|
      case ch
      when '"' then out << '\"'
      when '\\' then out << '\\\\'
      when "\n" then out << '\n'
      when "\r" then out << '\r'
      when "\t" then out << '\t'
      else out << ch
      end
    end
    out << '"'
    out
  end

  def self.parse(str)
    src = str.to_s
    return nil if src.empty?
    val, _ = parse_value(src, skip_ws(src, 0))
    val
  end

  def self.skip_ws(src, idx)
    len = src.length
    while idx < len
      c = src[idx]
      break unless c == " " || c == "\t" || c == "\n" || c == "\r"
      idx += 1
    end
    idx
  end

  def self.parse_value(src, idx)
    idx = skip_ws(src, idx)
    ch = src[idx]
    case ch
    when "{"
      parse_object(src, idx)
    when "["
      parse_array(src, idx)
    when '"'
      parse_string(src, idx)
    when "t"
      [true, idx + 4]
    when "f"
      [false, idx + 5]
    when "n"
      [nil, idx + 4]
    else
      parse_number(src, idx)
    end
  end

  def self.parse_object(src, idx)
    obj = {}
    idx = skip_ws(src, idx + 1)
    return [obj, idx + 1] if src[idx] == "}"
    len = src.length
    while idx < len
      idx = skip_ws(src, idx)
      key, idx = parse_string(src, idx)
      idx = skip_ws(src, idx)
      idx += 1 if src[idx] == ":"
      val, idx = parse_value(src, idx)
      obj[key] = val
      idx = skip_ws(src, idx)
      if src[idx] == ","
        idx += 1
      elsif src[idx] == "}"
        return [obj, idx + 1]
      else
        break
      end
    end
    [obj, idx]
  end

  def self.parse_array(src, idx)
    arr = []
    idx = skip_ws(src, idx + 1)
    return [arr, idx + 1] if src[idx] == "]"
    len = src.length
    while idx < len
      val, idx = parse_value(src, idx)
      arr << val
      idx = skip_ws(src, idx)
      if src[idx] == ","
        idx += 1
      elsif src[idx] == "]"
        return [arr, idx + 1]
      else
        break
      end
    end
    [arr, idx]
  end

  def self.parse_string(src, idx)
    idx += 1
    out = ""
    len = src.length
    while idx < len
      ch = src[idx]
      if ch == '"'
        return [out, idx + 1]
      elsif ch == "\\"
        idx += 1
        esc = src[idx]
        case esc
        when '"', "\\", "/" then out << esc
        when "n" then out << "\n"
        when "r" then out << "\r"
        when "t" then out << "\t"
        when "b" then out << "\b"
        when "f" then out << "\f"
        when "u"
          hex = src[idx + 1, 4]
          out << [hex.to_i(16)].pack("U") if hex && hex.length == 4
          idx += 4
        else
          out << esc.to_s
        end
      else
        out << ch
      end
      idx += 1
    end
    [out, idx]
  end

  def self.parse_number(src, idx)
    start = idx
    len = src.length
    is_float = false
    while idx < len
      c = src[idx]
      if c == "." || c == "e" || c == "E"
        is_float = true
        idx += 1
      elsif c == "-" || c == "+" || (c >= "0" && c <= "9")
        idx += 1
      else
        break
      end
    end
    tok = src[start, idx - start]
    val = is_float ? tok.to_f : tok.to_i
    [val, idx]
  end
end

module RPGCreatorHost
  def self.call(op, payload = {})
    raw = _call(op.to_s, RPGCJSON.dump(payload))
    return {} if raw.nil? || raw.empty?
    RPGCJSON.parse(raw)
  end
end

class MatchData
  attr_reader :string, :regexp, :captures
  def initialize(str, rx, start_idx, end_idx, groups)
    @string = str
    @regexp = rx
    @start_idx = start_idx.to_i
    @end_idx = end_idx.to_i
    @groups = groups || []
    @captures = @groups.size > 1 ? @groups[1..-1] : []
  end
  def [](idx); @groups[idx]; end
  def to_s; @groups[0].to_s; end
  def to_a; @groups.dup; end
  def size; @groups.size; end
  def length; @groups.size; end
  def begin(idx = 0); idx == 0 ? @start_idx : nil; end
  def end(idx = 0); idx == 0 ? @end_idx : nil; end
  def pre_match; @string[0, @start_idx] || ""; end
  def post_match; @string[@end_idx..-1] || ""; end
end

class Regexp
  IGNORECASE = 1
  EXTENDED = 2
  MULTILINE = 4
  @@last_match = nil

  def self.last_match(idx = nil)
    idx.nil? ? @@last_match : (@@last_match ? @@last_match[idx] : nil)
  end

  def self.compile(pattern, flags = nil)
    new(pattern, flags)
  end

  def self.quote(str)
    s = +""
    str.to_s.each_char do |c|
      s << "\\" if "\\^$.|?*+()[]{}".include?(c)
      s << c
    end
    s
  end
  class << self; alias escape quote; end

  attr_reader :source, :options
  def initialize(pattern, flags = nil)
    @source = pattern.is_a?(Regexp) ? pattern.source : pattern.to_s
    @flags = if flags.is_a?(Integer)
      f = +""
      f << "i" if (flags & IGNORECASE) != 0
      f << "m" if (flags & MULTILINE) != 0
      f << "x" if (flags & EXTENDED) != 0
      f
    else
      flags.to_s
    end
    @options = 0
  end

  def casefold?; @flags.include?("i"); end
  def inspect; "/#{@source}/#{@flags}"; end
  def to_s; "(?#{@flags}:#{@source})"; end
  def ==(other); other.is_a?(Regexp) && other.source == @source && other.inspect == inspect; end

  def _host_find(str, all: false, pos: 0)
    res = RPGCreatorHost.call("regex_match", {
      "pattern" => @source,
      "flags" => @flags,
      "text" => str.to_s,
      "pos" => pos.to_i,
      "all" => !!all,
    })
    (res["matches"] || []).map { |m| MatchData.new(str.to_s, self, m["start"], m["end"], m["groups"]) }
  end

  def match(str, pos = 0)
    return nil if str.nil?
    md = _host_find(str, all: false, pos: pos).first
    @@last_match = md
    md
  end

  def match?(str, pos = 0)
    !match(str, pos).nil?
  end

  def =~(str)
    md = match(str)
    md ? md.begin(0) : nil
  end

  def ===(str)
    match?(str)
  end
end

class String
  alias_method :_mrb_orig_sub, :sub
  alias_method :_mrb_orig_gsub, :gsub
  alias_method :_mrb_orig_split, :split

  def match(pattern, pos = 0)
    rx = pattern.is_a?(Regexp) ? pattern : Regexp.new(pattern.to_s)
    rx.match(self, pos)
  end

  def match?(pattern, pos = 0)
    !match(pattern, pos).nil?
  end

  def =~(pattern)
    pattern.is_a?(Regexp) ? (pattern =~ self) : Regexp.new(pattern.to_s) =~ self
  end

  def scan(pattern, &block)
    rx = pattern.is_a?(Regexp) ? pattern : Regexp.new(Regexp.quote(pattern.to_s))
    items = rx._host_find(self, all: true).map do |md|
      md.captures.empty? ? md.to_s : md.captures
    end
    if block
      items.each { |it| block.call(it) }
      self
    else
      items
    end
  end

  def _rpgc_expand_rep(rep, md)
    return rep unless rep.include?("\\")
    out = +""
    i = 0
    while i < rep.length
      if rep[i] == "\\" && i + 1 < rep.length
        nxt = rep[i + 1]
        if nxt >= "0" && nxt <= "9"
          out << (md[nxt.to_i] || "")
          i += 2
          next
        elsif nxt == "&"
          out << md.to_s
          i += 2
          next
        end
      end
      out << rep[i]
      i += 1
    end
    out
  end

  def sub(pattern, rep = nil, &block)
    return _mrb_orig_sub(pattern, rep, &block) unless pattern.is_a?(Regexp)
    md = pattern.match(self)
    return dup if md.nil?
    val = block ? block.call(md.to_s).to_s : _rpgc_expand_rep(rep.to_s, md)
    md.pre_match + val + md.post_match
  end

  def sub!(pattern, rep = nil, &block)
    res = sub(pattern, rep, &block)
    return nil if res == self && !(pattern.is_a?(Regexp) ? pattern.match?(self) : include?(pattern.to_s))
    replace(res)
  end

  def gsub(pattern, rep = nil, &block)
    return _mrb_orig_gsub(pattern, rep, &block) unless pattern.is_a?(Regexp)
    matches = pattern._host_find(self, all: true)
    return dup if matches.empty?
    out = +""
    cursor = 0
    matches.each do |md|
      s_idx = md.begin(0)
      e_idx = md.end(0)
      out << self[cursor, s_idx - cursor] if s_idx > cursor
      out << (block ? block.call(md.to_s).to_s : _rpgc_expand_rep(rep.to_s, md))
      cursor = e_idx
    end
    out << self[cursor..-1].to_s
    out
  end

  def gsub!(pattern, rep = nil, &block)
    return nil if (pattern.is_a?(Regexp) ? pattern._host_find(self, all: true).empty? : !include?(pattern.to_s))
    replace(gsub(pattern, rep, &block))
  end

  def split(pattern = " ", limit = 0)
    return _mrb_orig_split(pattern, limit) unless pattern.is_a?(Regexp)
    matches = pattern._host_find(self, all: true)
    return [dup] if matches.empty?
    out = []
    cursor = 0
    matches.each do |md|
      break if limit > 0 && out.size + 1 >= limit
      out << self[cursor, md.begin(0) - cursor].to_s
      cursor = md.end(0)
    end
    out << self[cursor..-1].to_s
    out
  end
end

class Color
  attr_accessor :red, :green, :blue, :alpha
  def initialize(r = 0.0, g = 0.0, b = 0.0, a = 255.0)
    set(r, g, b, a)
  end
  def set(r, g = nil, b = nil, a = nil)
    if r.is_a?(Color)
      @red, @green, @blue, @alpha = r.red, r.green, r.blue, r.alpha
    else
      @red = [[r.to_f, 0.0].max, 255.0].min
      @green = [[g.to_f, 0.0].max, 255.0].min
      @blue = [[b.to_f, 0.0].max, 255.0].min
      @alpha = [[(a.nil? ? 255.0 : a.to_f), 0.0].max, 255.0].min
    end
    self
  end
  def empty; set(0, 0, 0, 0); end
  def to_a; [@red, @green, @blue, @alpha]; end
end

class Tone
  attr_accessor :red, :green, :blue, :gray
  def initialize(r = 0.0, g = 0.0, b = 0.0, gray = 0.0)
    set(r, g, b, gray)
  end
  def set(r, g = nil, b = nil, gray = nil)
    if r.is_a?(Tone)
      @red, @green, @blue, @gray = r.red, r.green, r.blue, r.gray
    else
      @red = [[r.to_f, -255.0].max, 255.0].min
      @green = [[g.to_f, -255.0].max, 255.0].min
      @blue = [[b.to_f, -255.0].max, 255.0].min
      @gray = [[(gray.nil? ? 0.0 : gray.to_f), 0.0].max, 255.0].min
    end
    self
  end
  def to_a; [@red, @green, @blue, @gray]; end
end

class Rect
  attr_reader :x, :y, :width, :height
  def initialize(x = 0, y = 0, width = 0, height = 0)
    @_on_change = nil
    set(x, y, width, height)
  end
  def _bind_change(&blk); @_on_change = blk; end
  def x=(v); @x = v.to_i; @_on_change.call(self) if @_on_change; @x; end
  def y=(v); @y = v.to_i; @_on_change.call(self) if @_on_change; @y; end
  def width=(v); @width = v.to_i; @_on_change.call(self) if @_on_change; @width; end
  def height=(v); @height = v.to_i; @_on_change.call(self) if @_on_change; @height; end
  def set(x, y = nil, width = nil, height = nil)
    if x.is_a?(Rect)
      @x, @y, @width, @height = x.x, x.y, x.width, x.height
    else
      @x = x.to_i
      @y = y.to_i
      @width = width.to_i
      @height = height.to_i
    end
    @_on_change.call(self) if @_on_change
    self
  end
  def empty; set(0, 0, 0, 0); end
  def to_a; [@x, @y, @width, @height]; end
end

class Table
  attr_reader :xsize, :ysize, :zsize, :dims
  def initialize(xsize = 1, ysize = nil, zsize = nil)
    @dims = ysize.nil? ? 1 : (zsize.nil? ? 2 : 3)
    @xsize = [0, xsize.to_i].max
    @ysize = ysize.nil? ? 1 : [0, ysize.to_i].max
    @zsize = zsize.nil? ? 1 : [0, zsize.to_i].max
    @data = Array.new(@xsize * @ysize * @zsize, 0)
  end
  def resize(nx, ny = nil, nz = nil)
    nx = [0, nx.to_i].max
    ny = ny.nil? ? @ysize : [0, ny.to_i].max
    nz = nz.nil? ? @zsize : [0, nz.to_i].max
    new_data = Array.new(nx * ny * nz, 0)
    [nz, @zsize].min.times do |z|
      [ny, @ysize].min.times do |y|
        [nx, @xsize].min.times do |x|
          new_data[(z * ny + y) * nx + x] = self[x, y, z]
        end
      end
    end
    @xsize, @ysize, @zsize = nx, ny, nz
    @data = new_data
    self
  end
  def [](x, y = 0, z = 0)
    return 0 if x < 0 || x >= @xsize || y < 0 || y >= @ysize || z < 0 || z >= @zsize
    @data[(z * @ysize + y) * @xsize + x] || 0
  end
  def []=(*args)
    val = args.pop.to_i & 0xFFFF
    val -= 0x10000 if val >= 0x8000
    x = args[0].to_i
    y = (args[1] || 0).to_i
    z = (args[2] || 0).to_i
    return val if x < 0 || x >= @xsize || y < 0 || y >= @ysize || z < 0 || z >= @zsize
    @data[(z * @ysize + y) * @xsize + x] = val
  end
  def _raw_data; @data; end
  def _load_raw(xs, ys, zs, dims, arr)
    @xsize, @ysize, @zsize, @dims = xs.to_i, ys.to_i, zs.to_i, dims.to_i
    @data = arr.map(&:to_i)
    self
  end
end

class Font
  @@default_name = ["Malgun Gothic", "Arial"]
  @@default_size = 20
  @@default_bold = false
  @@default_italic = false
  @@default_shadow = true
  @@default_outline = true
  @@default_color = Color.new(255, 255, 255, 255)
  @@default_out_color = Color.new(0, 0, 0, 128)

  def self.default_name; @@default_name; end
  def self.default_name=(v); @@default_name = v; end
  def self.default_size; @@default_size; end
  def self.default_size=(v); @@default_size = v.to_i; end
  def self.default_bold; @@default_bold; end
  def self.default_bold=(v); @@default_bold = !!v; end
  def self.default_italic; @@default_italic; end
  def self.default_italic=(v); @@default_italic = !!v; end
  def self.default_shadow; @@default_shadow; end
  def self.default_shadow=(v); @@default_shadow = !!v; end
  def self.default_outline; @@default_outline; end
  def self.default_outline=(v); @@default_outline = !!v; end
  def self.default_color; @@default_color; end
  def self.default_color=(v); @@default_color = v; end
  def self.default_out_color; @@default_out_color; end
  def self.default_out_color=(v); @@default_out_color = v; end
  def self.exist?(name); true; end

  attr_accessor :name, :size, :bold, :italic, :shadow, :outline, :color, :out_color
  def initialize(name = @@default_name, size = @@default_size)
    @name = name
    @size = size.to_i
    @bold = @@default_bold
    @italic = @@default_italic
    @shadow = @@default_shadow
    @outline = @@default_outline
    @color = Color.new(@@default_color.red, @@default_color.green, @@default_color.blue, @@default_color.alpha)
    @out_color = Color.new(@@default_out_color.red, @@default_out_color.green, @@default_out_color.blue, @@default_out_color.alpha)
  end
end

class Bitmap
  attr_reader :_host_id, :width, :height
  attr_accessor :font

  def initialize(w_or_path, h = nil)
    if w_or_path.is_a?(String)
      res = RPGCreatorHost.call("bitmap_new", { "path" => w_or_path })
    else
      res = RPGCreatorHost.call("bitmap_new", { "width" => w_or_path.to_i, "height" => (h || 1).to_i })
    end
    @_host_id = res["id"].to_i
    @width = res["width"].to_i
    @height = res["height"].to_i
    @disposed = false
    @font = Font.new
  end

  def self._wrap_existing(hid, w, h)
    obj = allocate
    obj.instance_variable_set(:@_host_id, hid.to_i)
    obj.instance_variable_set(:@width, w.to_i)
    obj.instance_variable_set(:@height, h.to_i)
    obj.instance_variable_set(:@disposed, false)
    obj.instance_variable_set(:@font, Font.new)
    obj
  end

  def rect; Rect.new(0, 0, @width, @height); end
  def disposed?; @disposed; end
  def dispose
    return if @disposed
    RPGCreatorHost.call("bitmap_op", { "id" => @_host_id, "fn" => "dispose" })
    @disposed = true
  end
  def clear; RPGCreatorHost.call("bitmap_op", { "id" => @_host_id, "fn" => "clear" }); end
  def fill_rect(*args)
    if args[0].is_a?(Rect)
      r, col = args[0], args[1]
      x, y, w, h = r.x, r.y, r.width, r.height
    else
      x, y, w, h, col = args[0], args[1], args[2], args[3], args[4]
    end
    RPGCreatorHost.call("bitmap_op", { "id" => @_host_id, "fn" => "fill_rect", "x" => x.to_i, "y" => y.to_i, "w" => w.to_i, "h" => h.to_i, "color" => col.to_a })
  end
  def gradient_fill_rect(*args)
    if args[0].is_a?(Rect)
      r, c1, c2, vert = args[0], args[1], args[2], !!args[3]
      x, y, w, h = r.x, r.y, r.width, r.height
    else
      x, y, w, h, c1, c2, vert = args[0], args[1], args[2], args[3], args[4], args[5], !!args[6]
    end
    RPGCreatorHost.call("bitmap_op", { "id" => @_host_id, "fn" => "gradient_fill_rect", "x" => x.to_i, "y" => y.to_i, "w" => w.to_i, "h" => h.to_i, "color1" => c1.to_a, "color2" => c2.to_a, "vertical" => vert })
  end
  def draw_text(*args)
    if args[0].is_a?(Rect)
      r, str, align = args[0], args[1].to_s, (args[2] || 0).to_i
      x, y, w, h = r.x, r.y, r.width, r.height
    else
      x, y, w, h, str, align = args[0], args[1], args[2], args[3], args[4].to_s, (args[5] || 0).to_i
    end
    RPGCreatorHost.call("bitmap_op", {
      "id" => @_host_id, "fn" => "draw_text",
      "x" => x.to_i, "y" => y.to_i, "w" => w.to_i, "h" => h.to_i,
      "text" => str, "align" => align,
      "font" => { "size" => @font.size, "bold" => @font.bold, "color" => @font.color.to_a }
    })
  end
  def text_size(str)
    r = RPGCreatorHost.call("bitmap_op", { "id" => @_host_id, "fn" => "text_size", "text" => str.to_s })
    Rect.new(r["x"].to_i, r["y"].to_i, r["width"].to_i, r["height"].to_i)
  end
  def blt(dx, dy, src_bmp, src_rect, opacity = 255)
    RPGCreatorHost.call("bitmap_op", { "id" => @_host_id, "fn" => "blt", "x" => dx.to_i, "y" => dy.to_i, "src_id" => src_bmp._host_id, "src_rect" => src_rect.to_a, "opacity" => opacity.to_i })
  end
  def hue_change(hue)
    RPGCreatorHost.call("bitmap_op", { "id" => @_host_id, "fn" => "hue_change", "hue" => hue.to_f })
  end
  def get_pixel(x, y)
    c = RPGCreatorHost.call("bitmap_op", { "id" => @_host_id, "fn" => "get_pixel", "x" => x.to_i, "y" => y.to_i })
    Color.new(*c)
  end
  def set_pixel(x, y, col)
    RPGCreatorHost.call("bitmap_op", { "id" => @_host_id, "fn" => "set_pixel", "x" => x.to_i, "y" => y.to_i, "color" => col.to_a })
  end
end

class Viewport
  attr_reader :_host_id, :rect, :visible, :z, :ox, :oy, :color, :tone
  def initialize(*args)
    if args.empty?
      x, y, w, h = 0, 0, Graphics.width, Graphics.height
    elsif args[0].is_a?(Rect)
      r = args[0]
      x, y, w, h = r.x, r.y, r.width, r.height
    else
      x, y, w, h = args[0].to_i, args[1].to_i, args[2].to_i, args[3].to_i
    end
    res = RPGCreatorHost.call("viewport_new", { "x" => x, "y" => y, "width" => w, "height" => h })
    @_host_id = res["id"].to_i
    @rect = Rect.new(x, y, w, h)
    @visible = true
    @z, @ox, @oy = 0, 0, 0
    @color = Color.new(0, 0, 0, 0)
    @tone = Tone.new(0, 0, 0, 0)
    @disposed = false
  end
  def visible=(v); @visible = !!v; _sync("visible" => @visible); end
  def z=(v); @z = v.to_i; _sync("z" => @z); end
  def ox=(v); @ox = v.to_i; _sync("ox" => @ox); end
  def oy=(v); @oy = v.to_i; _sync("oy" => @oy); end
  def rect=(r); @rect = r; _sync("rect" => r.to_a); end
  def update; end
  def flash(color, duration); end
  def disposed?; @disposed; end
  def dispose
    return if @disposed
    RPGCreatorHost.call("obj_sync", { "id" => @_host_id, "dispose" => true })
    @disposed = true
  end
  def _sync(props)
    RPGCreatorHost.call("obj_sync", { "id" => @_host_id, "props" => props }) unless @disposed
  end
end

class Sprite
  attr_reader :_host_id, :viewport, :bitmap, :src_rect, :visible, :x, :y, :z, :ox, :oy, :zoom_x, :zoom_y, :angle, :mirror, :opacity, :blend_type, :bush_depth, :bush_opacity, :color, :tone
  def initialize(viewport = nil)
    @viewport = viewport
    res = RPGCreatorHost.call("sprite_new", { "viewport_id" => viewport ? viewport._host_id : nil })
    @_host_id = res["id"].to_i
    @bitmap = nil
    @src_rect = Rect.new(0, 0, 0, 0)
    @visible = true
    @x, @y, @z, @ox, @oy = 0, 0, 0, 0, 0
    @zoom_x, @zoom_y, @angle = 1.0, 1.0, 0.0
    @mirror = false
    @opacity, @blend_type = 255, 0
    @bush_depth, @bush_opacity = 0, 128
    @color = Color.new(0, 0, 0, 0)
    @tone = Tone.new(0, 0, 0, 0)
    @disposed = false
  end
  def bitmap=(bm)
    @bitmap = bm
    @src_rect = bm ? Rect.new(0, 0, bm.width, bm.height) : Rect.new(0, 0, 0, 0)
    @src_rect._bind_change { |r| _sync("src_rect" => r.to_a) }
    _sync("bitmap_id" => (bm ? bm._host_id : nil), "src_rect" => @src_rect.to_a)
  end
  def src_rect=(r)
    @src_rect = r
    @src_rect._bind_change { |nr| _sync("src_rect" => nr.to_a) } if @src_rect
    _sync("src_rect" => r.to_a)
  end
  def visible=(v); @visible = !!v; _sync("visible" => @visible); end
  def x=(v); @x = v.to_i; _sync("x" => @x); end
  def y=(v); @y = v.to_i; _sync("y" => @y); end
  def z=(v); @z = v.to_i; _sync("z" => @z); end
  def ox=(v); @ox = v.to_i; _sync("ox" => @ox); end
  def oy=(v); @oy = v.to_i; _sync("oy" => @oy); end
  def zoom_x=(v); @zoom_x = v.to_f; _sync("zoom_x" => @zoom_x); end
  def zoom_y=(v); @zoom_y = v.to_f; _sync("zoom_y" => @zoom_y); end
  def angle=(v); @angle = v.to_f; _sync("angle" => @angle); end
  def mirror=(v); @mirror = !!v; _sync("mirror" => @mirror); end
  def opacity=(v); @opacity = v.to_i; _sync("opacity" => @opacity); end
  def blend_type=(v); @blend_type = v.to_i; _sync("blend_type" => @blend_type); end
  def bush_depth=(v); @bush_depth = v.to_i; _sync("bush_depth" => @bush_depth); end
  def bush_opacity=(v); @bush_opacity = v.to_i; _sync("bush_opacity" => @bush_opacity); end
  def color=(v); @color = v; end
  def tone=(v); @tone = v; end
  def width; @src_rect.width; end
  def height; @src_rect.height; end
  def flash(color, duration); end
  def update; end
  def disposed?; @disposed; end
  def dispose
    return if @disposed
    RPGCreatorHost.call("obj_sync", { "id" => @_host_id, "dispose" => true })
    @disposed = true
  end
  def _sync(props)
    RPGCreatorHost.call("obj_sync", { "id" => @_host_id, "props" => props }) unless @disposed
  end
end

class Plane
  attr_reader :_host_id, :viewport, :bitmap, :visible, :z, :ox, :oy, :zoom_x, :zoom_y, :opacity, :blend_type, :color, :tone
  def initialize(viewport = nil)
    @viewport = viewport
    res = RPGCreatorHost.call("plane_new", { "viewport_id" => viewport ? viewport._host_id : nil })
    @_host_id = res["id"].to_i
    @bitmap = nil
    @visible = true
    @z, @ox, @oy = 0, 0, 0
    @zoom_x, @zoom_y = 1.0, 1.0
    @opacity, @blend_type = 255, 0
    @color = Color.new(0, 0, 0, 0)
    @tone = Tone.new(0, 0, 0, 0)
    @disposed = false
  end
  def bitmap=(bm); @bitmap = bm; _sync("bitmap_id" => (bm ? bm._host_id : nil)); end
  def visible=(v); @visible = !!v; _sync("visible" => @visible); end
  def z=(v); @z = v.to_i; _sync("z" => @z); end
  def ox=(v); @ox = v.to_i; _sync("ox" => @ox); end
  def oy=(v); @oy = v.to_i; _sync("oy" => @oy); end
  def opacity=(v); @opacity = v.to_i; _sync("opacity" => @opacity); end
  def blend_type=(v); @blend_type = v.to_i; _sync("blend_type" => @blend_type); end
  def zoom_x=(v); @zoom_x = v.to_f; end
  def zoom_y=(v); @zoom_y = v.to_f; end
  def color=(v); @color = v; end
  def tone=(v); @tone = v; end
  def disposed?; @disposed; end
  def dispose
    return if @disposed
    RPGCreatorHost.call("obj_sync", { "id" => @_host_id, "dispose" => true })
    @disposed = true
  end
  def _sync(props)
    RPGCreatorHost.call("obj_sync", { "id" => @_host_id, "props" => props }) unless @disposed
  end
end

class Window
  attr_reader :_host_id, :viewport, :windowskin, :contents, :cursor_rect, :active, :visible, :pause, :x, :y, :width, :height, :z, :ox, :oy, :padding, :padding_bottom, :openness, :opacity, :back_opacity, :contents_opacity, :tone, :arrows_visible
  def initialize(*args)
    if args.size == 4
      @viewport = nil
      @x, @y, @width, @height = args.map(&:to_i)
      res = RPGCreatorHost.call("window_new", { "x" => @x, "y" => @y, "width" => @width, "height" => @height })
    else
      @viewport = args[0]
      @x, @y, @width, @height = 0, 0, 0, 0
      res = RPGCreatorHost.call("window_new", { "viewport_id" => @viewport ? @viewport._host_id : nil })
    end
    @_host_id = res["id"].to_i
    @contents = Bitmap._wrap_existing(res["contents_id"].to_i, [1, @width - 32].max, [1, @height - 32].max)
    @windowskin = nil
    @cursor_rect = Rect.new(0, 0, 0, 0)
    @active, @visible, @pause, @arrows_visible = true, true, false, true
    @z, @ox, @oy = 100, 0, 0
    @padding, @padding_bottom = 12, 12
    @openness, @opacity, @back_opacity, @contents_opacity = 255, 255, 200, 255
    @tone = Tone.new(0, 0, 0, 0)
    @disposed = false
  end
  def windowskin=(bm); @windowskin = bm; _sync("windowskin_id" => (bm ? bm._host_id : nil)); end
  def contents=(bm); @contents = bm; _sync("contents_id" => (bm ? bm._host_id : nil)); end
  def visible=(v); @visible = !!v; _sync("visible" => @visible); end
  def active=(v); @active = !!v; _sync("active" => @active); end
  def pause=(v); @pause = !!v; _sync("pause" => @pause); end
  def x=(v); @x = v.to_i; _sync("x" => @x); end
  def y=(v); @y = v.to_i; _sync("y" => @y); end
  def width=(v); @width = v.to_i; _sync("width" => @width); end
  def height=(v); @height = v.to_i; _sync("height" => @height); end
  def z=(v); @z = v.to_i; _sync("z" => @z); end
  def ox=(v); @ox = v.to_i; _sync("ox" => @ox); end
  def oy=(v); @oy = v.to_i; _sync("oy" => @oy); end
  def openness=(v); @openness = v.to_i; _sync("openness" => @openness); end
  def opacity=(v); @opacity = v.to_i; _sync("opacity" => @opacity); end
  def back_opacity=(v); @back_opacity = v.to_i; _sync("back_opacity" => @back_opacity); end
  def contents_opacity=(v); @contents_opacity = v.to_i; _sync("contents_opacity" => @contents_opacity); end
  def padding=(v); @padding = v.to_i; _sync("padding" => @padding); end
  def padding_bottom=(v); @padding_bottom = v.to_i; _sync("padding_bottom" => @padding_bottom); end
  def cursor_rect=(r); @cursor_rect = r; end
  def tone=(t); @tone = t; end
  def arrows_visible=(v); @arrows_visible = !!v; end
  def update; end
  def disposed?; @disposed; end
  def dispose
    return if @disposed
    RPGCreatorHost.call("obj_sync", { "id" => @_host_id, "dispose" => true })
    @disposed = true
  end
  def _sync(props)
    RPGCreatorHost.call("obj_sync", { "id" => @_host_id, "props" => props }) unless @disposed
  end
end

class Tilemap
  attr_reader :_host_id, :viewport, :tileset, :autotiles, :bitmaps, :map_data, :priorities, :flags, :flash_data, :visible, :ox, :oy
  def initialize(viewport = nil)
    @viewport = viewport
    res = RPGCreatorHost.call("tilemap_new", { "viewport_id" => viewport ? viewport._host_id : nil })
    @_host_id = res["id"].to_i
    @autotiles = Array.new(7, nil)
    @bitmaps = Array.new(9, nil)
    @tileset = nil
    @map_data = nil
    @priorities = nil
    @flags = nil
    @flash_data = nil
    @visible = true
    @ox, @oy = 0, 0
    @disposed = false
  end
  def visible=(v); @visible = !!v; _sync("visible" => @visible); end
  def ox=(v); @ox = v.to_i; _sync("ox" => @ox); end
  def oy=(v); @oy = v.to_i; _sync("oy" => @oy); end
  def tileset=(bm); @tileset = bm; _sync("tileset_id" => (bm ? bm._host_id : nil)); end
  def map_data=(t); @map_data = t; _sync_table("map_data", t); end
  def priorities=(t); @priorities = t; _sync_table("priorities", t); end
  def flags=(t); @flags = t; _sync_table("flags", t); end
  def flash_data=(t); @flash_data = t; end
  def update
    return if @disposed
    _sync(
      "autotile_ids" => @autotiles.map { |b| b ? b._host_id : nil },
      "bitmap_ids" => @bitmaps.map { |b| b ? b._host_id : nil },
      "ox" => @ox,
      "oy" => @oy,
      "visible" => @visible
    )
  end
  def disposed?; @disposed; end
  def dispose
    return if @disposed
    RPGCreatorHost.call("obj_sync", { "id" => @_host_id, "dispose" => true })
    @disposed = true
  end
  def _sync(props)
    RPGCreatorHost.call("obj_sync", { "id" => @_host_id, "props" => props }) unless @disposed
  end
  def _sync_table(name, tbl)
    return if @disposed || tbl.nil?
    _sync(name => { "xsize" => tbl.xsize, "ysize" => tbl.ysize, "zsize" => tbl.zsize, "data" => tbl._raw_data })
  end
end

module Graphics
  def self.width; RPGCreatorHost.call("graphics_info")["width"].to_i; end
  def self.height; RPGCreatorHost.call("graphics_info")["height"].to_i; end
  def self.resize_screen(w, h); RPGCreatorHost.call("graphics_resize", { "width" => w.to_i, "height" => h.to_i }); end
  def self.frame_rate; RPGCreatorHost.call("graphics_info")["frame_rate"].to_i; end
  def self.frame_rate=(v); RPGCreatorHost.call("graphics_set", { "frame_rate" => v.to_i }); end
  def self.frame_count; RPGCreatorHost.call("graphics_info")["frame_count"].to_i; end
  def self.frame_count=(v); RPGCreatorHost.call("graphics_set", { "frame_count" => v.to_i }); end
  def self.brightness; RPGCreatorHost.call("graphics_info")["brightness"].to_i; end
  def self.brightness=(v); RPGCreatorHost.call("graphics_set", { "brightness" => v.to_i }); end
  def self.freeze; RPGCreatorHost.call("graphics_freeze"); end
  def self.transition(duration = 10, filename = "", vague = 40)
    RPGCreatorHost.call("graphics_transition", { "duration" => duration.to_i, "filename" => filename.to_s, "vague" => vague.to_i })
  end
  def self.update
    res = RPGCreatorHost.call("graphics_update")
    raise StopIteration, "Graphics host stopped" if res["continue"] == false
    nil
  end
  def self.wait(duration); duration.to_i.times { update }; end
  def self.fadeout(duration)
    d = [1, duration.to_i].max
    d.times { |i| self.brightness = 255 - (255 * (i + 1) / d); update }
  end
  def self.fadein(duration)
    d = [1, duration.to_i].max
    d.times { |i| self.brightness = (255 * (i + 1) / d); update }
  end
  def self.frame_reset; end
  def self.play_movie(_fn); end
end

module Input
  DOWN = 2; LEFT = 4; RIGHT = 6; UP = 8
  A = 11; B = 12; C = 13; X = 14; Y = 15; Z = 16; L = 17; R = 18
  SHIFT = 21; CTRL = 22; ALT = 23
  F5 = 25; F6 = 26; F7 = 27; F8 = 28; F9 = 29

  def self.update; end
  def self.press?(sym); !!RPGCreatorHost.call("input_state", { "kind" => "press", "sym" => sym.to_s }); end
  def self.trigger?(sym); !!RPGCreatorHost.call("input_state", { "kind" => "trigger", "sym" => sym.to_s }); end
  def self.repeat?(sym); !!RPGCreatorHost.call("input_state", { "kind" => "repeat", "sym" => sym.to_s }); end
  def self.dir4; RPGCreatorHost.call("input_state", { "kind" => "dir4" }).to_i; end
  def self.dir8; RPGCreatorHost.call("input_state", { "kind" => "dir8" }).to_i; end
end

module Audio
  def self.bgm_play(fn, vol = 100, pitch = 100, pos = 0)
    RPGCreatorHost.call("audio_cmd", { "fn" => "bgm_play", "filename" => fn.to_s, "volume" => vol.to_i, "pitch" => pitch.to_i, "pos" => pos.to_i })
  end
  def self.bgm_stop; RPGCreatorHost.call("audio_cmd", { "fn" => "bgm_stop" }); end
  def self.bgm_fade(ms); RPGCreatorHost.call("audio_cmd", { "fn" => "bgm_fade", "time" => ms.to_i }); end
  def self.bgm_pos; RPGCreatorHost.call("audio_cmd", { "fn" => "bgm_pos" }).to_i; end
  def self.bgs_play(fn, vol = 100, pitch = 100, pos = 0)
    RPGCreatorHost.call("audio_cmd", { "fn" => "bgs_play", "filename" => fn.to_s, "volume" => vol.to_i, "pitch" => pitch.to_i, "pos" => pos.to_i })
  end
  def self.bgs_stop; RPGCreatorHost.call("audio_cmd", { "fn" => "bgs_stop" }); end
  def self.bgs_fade(ms); RPGCreatorHost.call("audio_cmd", { "fn" => "bgs_fade", "time" => ms.to_i }); end
  def self.bgs_pos; RPGCreatorHost.call("audio_cmd", { "fn" => "bgs_pos" }).to_i; end
  def self.me_play(fn, vol = 100, pitch = 100)
    RPGCreatorHost.call("audio_cmd", { "fn" => "me_play", "filename" => fn.to_s, "volume" => vol.to_i, "pitch" => pitch.to_i })
  end
  def self.me_stop; RPGCreatorHost.call("audio_cmd", { "fn" => "me_stop" }); end
  def self.se_play(fn, vol = 100, pitch = 100)
    RPGCreatorHost.call("audio_cmd", { "fn" => "se_play", "filename" => fn.to_s, "volume" => vol.to_i, "pitch" => pitch.to_i })
  end
  def self.se_stop; RPGCreatorHost.call("audio_cmd", { "fn" => "se_stop" }); end
end

module Zlib
  module Inflate
    def self.inflate(data)
      res = RPGCreatorHost.call("zlib_inflate", { "b64" => RPGCBase64.encode(data) })
      RPGCBase64.decode(res["b64"].to_s)
    end
  end
  module Deflate
    def self.deflate(data)
      res = RPGCreatorHost.call("zlib_deflate", { "b64" => RPGCBase64.encode(data) })
      RPGCBase64.decode(res["b64"].to_s)
    end
  end
end

module RPG
  class DynamicObject
    def initialize(fields = {})
      fields.each do |k, v|
        instance_variable_set(:"@#{k}", v)
        define_singleton_method(k.to_sym) { instance_variable_get(:"@#{k}") }
        define_singleton_method(:"#{k}=") { |nv| instance_variable_set(:"@#{k}", nv) }
      end
    end
  end

  def self._ensure_class(class_path)
    parts = class_path.to_s.sub(/^RPG::/, "").split("::")
    cur = RPG
    parts.each do |part|
      if cur.const_defined?(part, false)
        cur = cur.const_get(part)
      else
        cls = Class.new(DynamicObject)
        cur.const_set(part, cls)
        cur = cls
      end
    end
    cur
  end

  def self.hydrate(val)
    case val
    when Array
      val.map { |x| hydrate(x) }
    when Hash
      t = val["__type__"]
      if t == "Table"
        xs = (val["xsize"] || 1).to_i
        ys = (val["ysize"] || 1).to_i
        zs = (val["zsize"] || 1).to_i
        dims = (val["dims"] || 1).to_i
        return Table.new._load_raw(xs, ys, zs, dims, val["data"] || [])
      elsif t == "Color"
        return Color.new(*(val["rgba"] || [0, 0, 0, 255]))
      elsif t == "Tone"
        return Tone.new(*(val["rgba"] || [0, 0, 0, 0]))
      elsif t == "int_map"
        h = {}
        (val["entries"] || []).each do |pair|
          h[hydrate(pair[0])] = hydrate(pair[1]) if pair.is_a?(Array) && pair.size >= 2
        end
        return h
      elsif t == "RubyObject" || val.key?("__ruby_class__")
        cls_name = val["class"] || val["__ruby_class__"]
        cls = _ensure_class(cls_name)
        raw_fields = val["fields"] || {}
        hydrated_fields = {}
        raw_fields.each { |k, v| hydrated_fields[k.to_s.sub(/^@/, "")] = hydrate(v) }
        return cls.new(hydrated_fields)
      else
        out = {}
        val.each { |k, v| out[k] = hydrate(v) }
        return out
      end
    else
      val
    end
  end
end

module FileTest
  def self.exist?(path)
    !!RPGCreatorHost.call("file_exist", { "path" => path.to_s })["exists"]
  end
  def self.file?(path); exist?(path); end
end

module Marshal
  def self.load(str_or_io)
    raw = str_or_io.respond_to?(:read) ? str_or_io.read : str_or_io.to_s
    data = RPGCreatorHost.call("marshal_loads_b64", { "b64" => RPGCBase64.encode(raw) })
    RPG.hydrate(data)
  end
end

def load_data(filename)
  data = RPGCreatorHost.call("load_data_json", { "path" => filename.to_s })
  RPG.hydrate(data)
end

def rgss_main(&block)
  begin
    block.call if block
  rescue RGSSReset
    retry
  rescue StopIteration
    nil
  end
end

def rgss_stop
  nil
end

def puts(*args)
  args.each { |a| RPGCreatorHost.call("log", { "message" => a.to_s }) }
  nil
end

def p(*args)
  args.each { |a| RPGCreatorHost.call("log", { "message" => a.inspect }) }
  args.size <= 1 ? args.first : args
end

def msgbox(*args)
  puts(*args)
end

def msgbox_p(*args)
  p(*args)
end
